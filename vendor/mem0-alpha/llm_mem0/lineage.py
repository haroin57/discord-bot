"""Durable consolidation plans and explicit support lineage, additive local state.

The primary memory provider remains canonical. SQLite stores repair intent before
metadata updates; retries never manufacture support for unknown legacy metadata.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import time

from .settings import settings
from .sqlite_store import SqliteStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consolidation_jobs (
 namespace TEXT NOT NULL, operation_id TEXT NOT NULL, user_id TEXT NOT NULL,
 payload TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(namespace,operation_id));
CREATE TABLE IF NOT EXISTS derivation_sources (
 namespace TEXT NOT NULL, derived_id TEXT NOT NULL, source_id TEXT NOT NULL,
 user_id TEXT NOT NULL, kind TEXT NOT NULL, rule TEXT NOT NULL,
 PRIMARY KEY(namespace,derived_id,source_id));
CREATE INDEX IF NOT EXISTS derivation_source_lookup ON derivation_sources(namespace,source_id);
CREATE TABLE IF NOT EXISTS lineage_retirements (
 namespace TEXT NOT NULL, fact_id TEXT NOT NULL, retired_at INTEGER NOT NULL,
 PRIMARY KEY(namespace,fact_id));
CREATE TABLE IF NOT EXISTS lineage_repairs (
 namespace TEXT NOT NULL, derived_id TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(namespace,derived_id));
CREATE TABLE IF NOT EXISTS lineage_discovery (
 namespace TEXT NOT NULL, source_id TEXT NOT NULL, user_id TEXT NOT NULL,
 offset INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(namespace,source_id));
CREATE TABLE IF NOT EXISTS lineage_propagation (
 namespace TEXT NOT NULL, source_id TEXT NOT NULL, retired_at INTEGER NOT NULL,
 after_id TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(namespace,source_id));
CREATE TABLE IF NOT EXISTS lifecycle_work_order (
 namespace TEXT NOT NULL, kind TEXT NOT NULL, item_key TEXT NOT NULL, attempt INTEGER NOT NULL,
 PRIMARY KEY(namespace,kind,item_key));
CREATE TABLE IF NOT EXISTS lifecycle_attempt_counter (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), sequence INTEGER NOT NULL);
INSERT OR IGNORE INTO lifecycle_attempt_counter VALUES (1,0);
CREATE TABLE IF NOT EXISTS lineage_delete_intents (
 namespace TEXT NOT NULL, source_id TEXT NOT NULL, user_id TEXT NOT NULL,
 done INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(namespace,source_id));
"""
_stores: dict[str, SqliteStore] = {}
_stores_lock = threading.Lock()


def _namespace() -> str:
    return str(settings.MEM0_COLLECTION_NAME)


def _store() -> SqliteStore:
    path = str(Path(settings.STATE_DIR).resolve() / "mem0_lifecycle.sqlite")
    with _stores_lock:
        # The new state contains source snapshots. Never create it through a
        # symlink or with a permissive inherited umask.
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("lifecycle state must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        if path not in _stores:
            value = SqliteStore("mem0_lifecycle.sqlite", _SCHEMA)
            value.reset_path_for_test(path)
            _stores[path] = value
        return _stores[path]


def _sync_state_directory() -> None:
    """Persist newly created DB/WAL names before a destructive provider step."""
    directory = Path(settings.STATE_DIR).resolve()
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def current_validity(metadata: dict, *, now: float | None = None) -> bool:
    """Reject malformed/future/closed intervals without guessing missing dates."""
    now = time.time() if now is None else now
    try:
        start, end = metadata.get("valid_from"), metadata.get("valid_to")
        for value in (start, end):
            if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
                return False
        return (start is None or float(start) <= now) and (end is None or now < float(end))
    except (TypeError, ValueError, OverflowError):
        return False


def supported_interval(members: list[dict]) -> dict:
    starts = [(r.get("metadata") or {}).get("valid_from") for r in members]
    ends = [(r.get("metadata") or {}).get("valid_to") for r in members]
    interval = {}
    if any(v is not None for v in starts):
        interval["valid_from"] = max(float(v) for v in starts if v is not None)
    if any(v is not None for v in ends):
        interval["valid_to"] = min(float(v) for v in ends if v is not None)
    return interval


def snapshot(row: dict) -> str:
    """Changes to freshness/eligibility are as important as changes to wording."""
    md = row.get("metadata") or {}
    keys = ("tier", "importance", "attribute", "scope", "condition", "category", "subcategory",
            "valid_from", "valid_to", "mention_count", "recall_count", "reinforced_at",
            "last_confirmed_at", "confirmed_at", "last_recalled_at", "gist_of", "timestamp",
            "last_reinforced_at", "user_confirmed_at", "confirmation_revision", "reinforced_event_ids",
            "created_at_unix")
    return hashlib.sha256(json.dumps([
        row.get("memory") or row.get("text") or "", {k: md.get(k) for k in keys},
    ], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def save_job(operation_id: str, user_id: str, payload: dict, *, done: bool = False) -> None:
    with _store().connect() as c:
        c.execute("INSERT INTO consolidation_jobs VALUES (?,?,?,?,?) ON CONFLICT(namespace,operation_id) "
                  "DO UPDATE SET payload=excluded.payload,done=excluded.done",
                  (_namespace(), operation_id, str(user_id), json.dumps(payload, ensure_ascii=False), int(done)))
        c.commit()
        _sync_state_directory()


def pending_jobs(user_id: str, limit: int) -> list[dict]:
    with _store().connect() as c:
        rows = c.execute("SELECT operation_id,payload FROM consolidation_jobs "
                         "WHERE namespace=? AND user_id=? AND done=0 ORDER BY rowid LIMIT ?",
                         (_namespace(), str(user_id), max(0, limit))).fetchall()
        return [{"operation_id": r["operation_id"], **json.loads(r["payload"])} for r in rows]


@asynccontextmanager
async def consolidation_lock(user_id: str):
    """Cross-process nonblocking lease; OS releases it after a crashed owner."""
    import fcntl
    digest = hashlib.sha256(f"{_namespace()}:{user_id}".encode()).hexdigest()
    directory = Path(settings.STATE_DIR) / "consolidation_locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / (digest + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("consolidation busy; durable plan retained")
                await asyncio.sleep(0.02)
        yield
    finally:
        os.close(fd)


def _unsupported(c, derived_id: str) -> bool:
    return _unsupported_at(c, derived_id) is not None


def _unsupported_at(c, derived_id: str) -> int | None:
    sources = c.execute("SELECT s.rule,r.retired_at FROM derivation_sources s LEFT JOIN lineage_retirements r "
                        "ON r.namespace=s.namespace AND r.fact_id=s.source_id "
                        "WHERE s.namespace=? AND s.derived_id=?", (_namespace(), derived_id)).fetchall()
    retired = [r["retired_at"] for r in sources if r["retired_at"] is not None]
    if not sources or not retired:
        return None
    if sources[0]["rule"] == "all":
        return min(retired)
    return max(retired) if len(retired) == len(sources) else None


def _touch_work(c, kind: str, item_key: str) -> None:
    c.execute("UPDATE lifecycle_attempt_counter SET sequence=sequence+1 WHERE singleton=1")
    sequence = c.execute("SELECT sequence FROM lifecycle_attempt_counter WHERE singleton=1").fetchone()[0]
    c.execute("INSERT OR REPLACE INTO lifecycle_work_order VALUES (?,?,?,?)",
              (_namespace(), kind, item_key, sequence))


def _enqueue_retirement(c, source_id: str, retired_at: int) -> None:
    previous = c.execute("SELECT retired_at FROM lineage_retirements WHERE namespace=? AND fact_id=?",
                         (_namespace(), source_id)).fetchone()
    c.execute("INSERT INTO lineage_retirements VALUES (?,?,?) ON CONFLICT(namespace,fact_id) "
              "DO UPDATE SET retired_at=min(retired_at,excluded.retired_at)", (_namespace(), source_id, retired_at))
    c.execute("INSERT OR IGNORE INTO lineage_propagation(namespace,source_id,retired_at) VALUES (?,?,?)",
              (_namespace(), source_id, retired_at))
    if previous and retired_at < previous[0]:
        c.execute("UPDATE lineage_propagation SET retired_at=?,after_id='',done=0 WHERE namespace=? AND source_id=?",
                  (retired_at, _namespace(), source_id))


def _drain_propagation(c, *, limit: int) -> int:
    """Bounded fanout pages; unfinished descendants remain durable work."""
    budget = max(0, min(limit, 1000))
    initial_budget = budget
    while budget:
        job = c.execute("SELECT p.* FROM lineage_propagation p LEFT JOIN lifecycle_work_order w "
                        "ON w.namespace=p.namespace AND w.kind='propagate' AND w.item_key=p.source_id "
                        "WHERE p.namespace=? AND p.done=0 ORDER BY COALESCE(w.attempt,0),p.rowid LIMIT 1",
                        (_namespace(),)).fetchone()
        if job is None:
            break
        rows = c.execute("SELECT DISTINCT derived_id FROM derivation_sources WHERE namespace=? AND source_id=? "
                         "AND derived_id>? ORDER BY derived_id LIMIT ?",
                         (_namespace(), job["source_id"], job["after_id"], budget + 1)).fetchall()
        selected = rows[:budget]
        for row in selected:
            lost_at = _unsupported_at(c, row[0])
            if lost_at is not None:
                c.execute("INSERT OR REPLACE INTO lineage_repairs VALUES (?,?,0)", (_namespace(), row[0]))
                _enqueue_retirement(c, row[0], lost_at)
        c.execute("UPDATE lineage_propagation SET after_id=?,done=? WHERE namespace=? AND source_id=?",
                  (selected[-1][0] if selected else job["after_id"], int(len(rows) <= len(selected)),
                   _namespace(), job["source_id"]))
        _touch_work(c, "propagate", job["source_id"])
        budget -= max(1, len(selected))
    return initial_budget - budget


def _pending_ancestor_gate(c, ids: list[str], instant: float) -> set[str]:
    """Finite ancestor resolution while materialized propagation is incomplete.

    Exceeding the node/depth budget or detecting corrupt cycles is fail-closed
    for these candidates, not evidence that their support remains valid.
    """
    memo = {}
    visiting = set()
    budget = [5000]
    def lost(fid, depth=0):
        if fid in memo:
            return memo[fid]
        if depth >= 64 or budget[0] <= 0 or fid in visiting:
            raise RuntimeError("lineage ancestor verification budget exceeded")
        budget[0] -= 1
        visiting.add(fid)
        retired = c.execute("SELECT retired_at FROM lineage_retirements WHERE namespace=? AND fact_id=?",
                            (_namespace(), fid)).fetchone()
        if retired and retired[0] <= instant:
            result = True
        else:
            sources = c.execute("SELECT source_id,rule FROM derivation_sources WHERE namespace=? AND derived_id=? LIMIT 1001",
                                (_namespace(), fid)).fetchall()
            if len(sources) > 1000:
                raise RuntimeError("lineage support verification budget exceeded")
            result = bool(sources) and (any(lost(r["source_id"], depth + 1) for r in sources)
                                        if sources[0]["rule"] == "all"
                                        else all(lost(r["source_id"], depth + 1) for r in sources))
        visiting.remove(fid)
        memo[fid] = result
        return result
    try:
        return {fid for fid in ids if lost(fid)}
    except RuntimeError:
        return set(ids)


def unsupported_fact_ids(fact_ids, *, as_of: float | None = None) -> set[str]:
    """Batched retrieval gate, including pending provider repairs.

    Retirement after an explicit historical as_of does not erase earlier support.
    Unknown lineage is not inferred. SQLite failures propagate rather than allow
    known-unsupported content to be treated as a verified empty result.
    """
    instant = time.time() if as_of is None else float(as_of)
    if not math.isfinite(instant):
        raise ValueError("finite as_of required")
    ids = list(dict.fromkeys(str(fid) for fid in fact_ids if fid))
    if not ids or not (Path(settings.STATE_DIR) / "mem0_lifecycle.sqlite").is_file():
        return set()
    unsupported = set()
    with _store().connect() as c:
        c.execute("BEGIN")
        if c.execute("SELECT 1 FROM lineage_propagation WHERE namespace=? AND done=0 LIMIT 1",
                     (_namespace(),)).fetchone():
            return _pending_ancestor_gate(c, ids, instant)
        for begin in range(0, len(ids), 500):
            chunk = ids[begin:begin + 500]
            marks = ",".join("?" for _ in chunk)
            retired = c.execute(f"SELECT fact_id,retired_at FROM lineage_retirements "
                                f"WHERE namespace=? AND fact_id IN ({marks})", (_namespace(), *chunk)).fetchall()
            unsupported.update(r["fact_id"] for r in retired if r["retired_at"] <= instant)
            rows = c.execute("SELECT s.derived_id,s.rule,r.retired_at FROM derivation_sources s "
                             "LEFT JOIN lineage_retirements r ON r.namespace=s.namespace AND r.fact_id=s.source_id "
                             f"WHERE s.namespace=? AND s.derived_id IN ({marks})", (_namespace(), *chunk)).fetchall()
            groups = {}
            for row in rows:
                groups.setdefault(row["derived_id"], []).append(row)
            for fid, sources in groups.items():
                retired_flags = [r["retired_at"] is not None and r["retired_at"] <= instant for r in sources]
                if (any(retired_flags) if sources[0]["rule"] == "all" else all(retired_flags)):
                    unsupported.add(fid)
    return unsupported


def register_derivation(derived_id: str, user_id: str, source_ids: list[str], *,
                        kind: str = "schema", rule: str = "any") -> bool:
    """Register explicit independent support; return False if already unsupported."""
    if not derived_id or not user_id or not source_ids or rule not in {"any", "all"}:
        raise ValueError("explicit lineage and support rule required")
    sources = sorted(set(str(value) for value in source_ids if value))
    with _store().connect() as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute("SELECT user_id,rule FROM derivation_sources WHERE namespace=? AND derived_id=? LIMIT 1",
                             (_namespace(), derived_id)).fetchone()
        if existing and (existing["user_id"], existing["rule"]) != (str(user_id), rule):
            raise ValueError("derived owner/support rule mismatch")
        for source in sources:
            # Follow derived→source links; UNION terminates even with corrupt legacy cycles.
            cycle = c.execute("""WITH RECURSIVE ancestors(id) AS (SELECT ? UNION
                SELECT s.source_id FROM derivation_sources s JOIN ancestors a ON s.derived_id=a.id
                WHERE s.namespace=?) SELECT 1 FROM ancestors WHERE id=? LIMIT 1""",
                              (source, _namespace(), derived_id)).fetchone()
            if cycle:
                raise ValueError("derived source cycle")
        for source in sources:
            c.execute("INSERT OR IGNORE INTO derivation_sources VALUES (?,?,?,?,?,?)",
                      (_namespace(), derived_id, source, str(user_id), kind, rule))
        unsupported = _unsupported(c, derived_id)
        if unsupported:
            c.execute("INSERT OR REPLACE INTO lineage_repairs VALUES (?,?,0)", (_namespace(), derived_id))
            _enqueue_retirement(c, derived_id, _unsupported_at(c, derived_id))
        c.commit()
        return not unsupported


def retire_source(source_id: str, *, user_id: str = "", retired_at: int | None = None,
                  propagation_limit: int = 100) -> None:
    """Commit a source tombstone before attempting provider-dependent repair."""
    with _store().connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _enqueue_retirement(c, source_id, int(time.time()) if retired_at is None else retired_at)
        _drain_propagation(c, limit=propagation_limit)
        if user_id:
            c.execute("INSERT OR IGNORE INTO lineage_discovery(namespace,source_id,user_id) VALUES (?,?,?)",
                      (_namespace(), source_id, str(user_id)))
        c.execute("UPDATE lineage_delete_intents SET done=1 WHERE namespace=? AND source_id=?",
                  (_namespace(), source_id))
        c.commit()


def prepare_source_delete(source_id: str, user_id: str = "") -> None:
    """Persist recovery identity BEFORE deleting the canonical provider row."""
    with _store().connect() as c:
        c.execute("INSERT INTO lineage_delete_intents VALUES (?,?,?,0) ON CONFLICT(namespace,source_id) "
                  "DO UPDATE SET user_id=CASE WHEN excluded.user_id!='' THEN excluded.user_id ELSE user_id END,done=0",
                  (_namespace(), str(source_id), str(user_id)))
        c.commit()
        # SQLite commit syncs its transaction. The parent directory entry for
        # a first-created DB/WAL also has to be durable before primary delete.
        _sync_state_directory()


def resolve_source_deletes_sync(mem, *, limit: int = 100) -> dict:
    """Resolve uncertain deletion under its source lock; plans alone never retire."""
    from .reinforce import memory_mutation_lock
    completed = failed = 0
    with _store().connect() as c:
        jobs = c.execute("SELECT d.source_id,d.user_id FROM lineage_delete_intents d LEFT JOIN lifecycle_work_order w "
                         "ON w.namespace=d.namespace AND w.kind='delete' AND w.item_key=d.source_id "
                         "WHERE d.namespace=? AND d.done=0 ORDER BY COALESCE(w.attempt,0),d.rowid LIMIT ?",
                         (_namespace(), max(0, min(limit, 1000)))).fetchall()
    for job in jobs:
        with _store().connect() as c:
            _touch_work(c, "delete", job["source_id"])
            c.commit()
        try:
            with memory_mutation_lock(job["source_id"]):
                # A live deletion may have completed while this worker waited.
                with _store().connect() as c:
                    pending = c.execute("SELECT done FROM lineage_delete_intents WHERE namespace=? AND source_id=?",
                                        (_namespace(), job["source_id"])).fetchone()
                if pending is None or pending[0]:
                    continue
                current = mem.get(job["source_id"])
                if current is None:
                    retire_source(job["source_id"], user_id=job["user_id"], propagation_limit=0)
                elif isinstance(current, dict) and current.get("id") == job["source_id"]:
                    with _store().connect() as c:
                        c.execute("UPDATE lineage_delete_intents SET done=1 WHERE namespace=? AND source_id=?",
                                  (_namespace(), job["source_id"]))
                        c.commit()
                else:
                    raise RuntimeError("deletion readback is indeterminate")
                completed += 1
        except Exception:
            failed += 1
    return {"completed": completed, "failed": failed, "pending": _pending_count()}


def _pending_count() -> int:
    with _store().connect() as c:
        return sum(c.execute(f"SELECT count(*) FROM {table} WHERE namespace=? AND done=0",
                             (_namespace(),)).fetchone()[0]
                   for table in ("lineage_repairs", "lineage_discovery", "lineage_propagation", "lineage_delete_intents"))


def has_pending_repairs() -> bool:
    path = Path(settings.STATE_DIR) / "mem0_lifecycle.sqlite"
    return path.is_file() and _pending_count() > 0


def _discover_legacy(mem, *, page_size: int) -> int:
    """Bounded prefix pagination for SDKs whose get_all has no offset.

    The 10,000-row cap is explicit. A larger/unstable legacy collection remains
    pending for operator reconciliation, never silently acknowledged as complete.
    """
    from .client import mem_get_all
    failures = 0
    with _store().connect() as c:
        jobs = c.execute("SELECT d.source_id,d.user_id,d.offset FROM lineage_discovery d LEFT JOIN lifecycle_work_order w "
                         "ON w.namespace=d.namespace AND w.kind='discover' AND w.item_key=d.source_id "
                         "WHERE d.namespace=? AND d.done=0 ORDER BY COALESCE(w.attempt,0),d.rowid LIMIT 4",
                         (_namespace(),)).fetchall()
    for job in jobs:
        with _store().connect() as c:
            _touch_work(c, "discover", job["source_id"])
            c.commit()
        try:
            offset = job["offset"]
            if offset >= 10000:
                failures += 1
                continue
            size = min(max(1, page_size), 250, 10000 - offset)
            response = mem_get_all(mem, user_id=job["user_id"], limit=offset + size + 1)
            rows = response.get("results") if isinstance(response, dict) else response
            if not isinstance(rows, list):
                raise RuntimeError("legacy lineage enumeration malformed")
            for row in rows[offset:offset + size]:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                md = row.get("metadata") or {}
                raw = md.get("derived_from")
                if isinstance(raw, str):
                    sources = [item.strip() for item in raw.split(",") if item.strip()]
                    if job["source_id"] in sources:
                        register_derivation(row["id"], job["user_id"], sources,
                                            kind=md.get("source") or "legacy_explicit")
            done = len(rows) <= offset + size
            with _store().connect() as c:
                c.execute("UPDATE lineage_discovery SET offset=?,done=? WHERE namespace=? AND source_id=?",
                          (offset + min(size, max(0, len(rows) - offset)), int(done), _namespace(), job["source_id"]))
                c.commit()
        except Exception:
            failures += 1
    return failures


def repair_dependents_sync(mem, *, limit: int = 100) -> dict:
    """Retry pending primary metadata updates; unsupported facts stay in repair intent."""
    from .reinforce import mutate_memory_metadata_sync
    if limit <= 0:
        return {"completed": 0, "failed": 0, "pending": _pending_count()}
    resolved = resolve_source_deletes_sync(mem, limit=limit)
    completed = resolved["completed"]
    with _store().connect() as c:
        c.execute("BEGIN IMMEDIATE")
        propagated = _drain_propagation(c, limit=limit)
        c.commit()
    failed = resolved["failed"] + _discover_legacy(mem, page_size=max(1, min(limit, 250)))
    if propagated < limit:
        with _store().connect() as c:
            c.execute("BEGIN IMMEDIATE")
            _drain_propagation(c, limit=limit - propagated)
            c.commit()
    visited = set()
    while len(visited) < max(0, limit):
        with _store().connect() as c:
            pending = c.execute("SELECT r.derived_id FROM lineage_repairs r LEFT JOIN lifecycle_work_order w "
                                "ON w.namespace=r.namespace AND w.kind='repair' AND w.item_key=r.derived_id "
                                "WHERE r.namespace=? AND r.done=0 ORDER BY COALESCE(w.attempt,0),r.rowid LIMIT ?",
                                (_namespace(), max(1, limit))).fetchall()
        ids = [r[0] for r in pending if r[0] not in visited]
        if not ids:
            break
        for derived_id in ids[:limit - len(visited)]:
            visited.add(derived_id)
            with _store().connect() as c:
                _touch_work(c, "repair", derived_id)
                c.commit()
            try:
                with _store().connect() as c:
                    retired_at = _unsupported_at(c, derived_id)
                    owner = c.execute("SELECT user_id FROM derivation_sources WHERE namespace=? AND derived_id=? LIMIT 1",
                                      (_namespace(), derived_id)).fetchone()
                if retired_at is not None:
                    def suspend(fresh):
                        md = fresh.get("metadata") or {}
                        fresh_owner = fresh.get("user_id") or md.get("user_id")
                        if owner is None or str(fresh_owner or "") != owner[0]:
                            raise RuntimeError("dependent owner no longer matches explicit lineage")
                        return {"valid_to": min(float(md["valid_to"]), retired_at) if md.get("valid_to") is not None else retired_at,
                                "derived_status": "unsupported"}
                    changed = mutate_memory_metadata_sync(mem, derived_id, suspend)
                    if not changed and mem.get(derived_id) is not None:
                        raise RuntimeError("dependent metadata could not be repaired")
                    with _store().connect() as c:
                        _enqueue_retirement(c, derived_id, retired_at)
                        c.commit()
                with _store().connect() as c:
                    c.execute("UPDATE lineage_repairs SET done=1 WHERE namespace=? AND derived_id=?",
                              (_namespace(), derived_id))
                    c.commit()
                completed += 1
            except Exception:
                failed += 1
    return {"completed": completed, "failed": failed, "pending": _pending_count()}


def retire_source_and_repair_sync(mem, source_id: str, user_id: str = "", *, limit: int = 100) -> dict:
    """Public-delete hook, called after confirmed primary deletion.

    Legacy explicit derived_from IDs can establish support, not names or text.
    Their independent-support rule is conservative: retain a surviving source.
    """
    retire_source(source_id, user_id=user_id)
    return repair_dependents_sync(mem, limit=limit)

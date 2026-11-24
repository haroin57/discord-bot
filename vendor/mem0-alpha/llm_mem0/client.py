"""Memory store singleton (mem0/Chroma) + low-level housekeeping (get_all/delete)."""

from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from contextvars import copy_context
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

from .auth import get_auth_backend
from .settings import settings

log = logging.getLogger(__name__)

# Back-compat module constants. The live values are settings.* (dynamic);
# these freeze at import for callers that import them directly, exactly as
# they always did.
#
# The Chroma adapter returns score = distance (small = similar).
# MEM0_RELEVANCE_MAX_DISTANCE is the pre-rerank gate applied inside
# search_memories() to drop candidates that are physically too far before
# the LLM rerank ever sees them; also the sanity gate for the rerank=False
# path in search_memories_smart.
MEM0_RELEVANCE_MAX_DISTANCE = settings.MEM0_RELEVANCE_MAX_DISTANCE
# DEPRECATED — see settings.py. Kept so env config that sets it still imports.
MEM0_FINAL_RELEVANCE_MAX_DISTANCE = settings.MEM0_FINAL_RELEVANCE_MAX_DISTANCE
# Back-compat alias for any caller still importing the old name. Semantic is
# "max distance" (not "min similarity") — do not flip it.
MEM0_RELEVANCE_THRESHOLD = MEM0_RELEVANCE_MAX_DISTANCE

_mem0_instance = None
_mem0_provider: str | None = None
# AuthBackend.provider_id() snapshot at build time — drift triggers rebuild.
_mem0_provider_id: str | None = None
_mem0_storage_config: tuple | None = None
# Last init failure timestamp (monotonic seconds). When set, _get_mem0()
# returns None without re-trying until _INIT_RETRY_COOLDOWN_SEC has elapsed.
# This allows automatic recovery after a transient ChromaDB outage instead of
# permanently disabling the store after the first failure.
_init_failed_at: float | None = None
_INIT_RETRY_COOLDOWN_SEC = 60.0
# _get_mem0 is called via asyncio.to_thread from several coroutines at once;
# without a lock two threads could both run the (multi-second) init.
_init_lock = threading.Lock()
_INGEST_RECEIPT_LOCKS = [threading.RLock() for _ in range(128)]


@contextmanager
def _ingest_receipt_lock(key: str):
    """Share one worker-only lock between insertion and explicit deletion.

    Acquire the memory-mutation stripe before this lock when both are needed.
    Separate lock files prevent collisions with memory-mutation stripes.
    """
    import fcntl

    stripe = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(_INGEST_RECEIPT_LOCKS)
    directory = Path(settings.STATE_DIR) / "ingest_receipt_locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _INGEST_RECEIPT_LOCKS[stripe]:
        fd = os.open(directory / f"{stripe}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _write_deleted_receipt(key: str) -> str | None:
    """Commit a tombstone before touching the provider; return the exact old row."""
    directory = Path(settings.STATE_DIR)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(directory / "mem0_ingest_receipts.sqlite3", timeout=30)) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, response TEXT NOT NULL)")
        row = db.execute("SELECT response FROM receipts WHERE key=?", (key,)).fetchone()
        db.execute("INSERT OR REPLACE INTO receipts VALUES (?, ?)",
                   (key, json.dumps({"deleted": True, "results": []})))
        db.commit()
    # Ensure the receipt file name is durable too, including first creation.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return row[0] if row else None


def _restore_deleted_receipt(key: str, previous: str | None) -> None:
    """Called only while holding the receipt lock after confirming no deletion."""
    path = Path(settings.STATE_DIR) / "mem0_ingest_receipts.sqlite3"
    with closing(sqlite3.connect(path, timeout=30)) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if previous is None:
            db.execute("DELETE FROM receipts WHERE key=?", (key,))
        else:
            db.execute("INSERT OR REPLACE INTO receipts VALUES (?, ?)", (key, previous))
        db.commit()


def _delete_primary_with_receipt(mem, memory_id: str) -> dict | None:
    """Keep uncertain outcomes suppressed instead of resurrecting a deleted fact."""
    getter = getattr(mem, "get", None)
    row = getter(memory_id) if callable(getter) else None
    metadata = row.get("metadata") if isinstance(row, dict) else None
    from . import lineage
    owner = (row.get("user_id") or (metadata or {}).get("user_id") or "") if isinstance(row, dict) else ""
    # Persist recovery authority before the primary can disappear. A failure
    # here must prevent deletion, not strand unsupported derived memories.
    lineage.prepare_source_delete(memory_id, str(owner))
    key = metadata.get("ingest_key") if isinstance(metadata, dict) else None
    if key is None or key == "":
        # Older rows have no ingest receipt. Retain the legacy delete API.
        mem.delete(memory_id)
        return row if isinstance(row, dict) else None
    if not isinstance(key, str):
        raise ValueError("invalid memory ingest key")
    with _ingest_receipt_lock(key):
        previous = _write_deleted_receipt(key)
        try:
            mem.delete(memory_id)
        except Exception:
            # A timeout can follow a committed deletion. Only a fresh positive
            # existence check permits undoing the tombstone. A crash/BaseException
            # or failed check keeps it durable so an old request cannot reinsert.
            try:
                current = getter(memory_id)
                current_meta = current.get("metadata") if isinstance(current, dict) else None
                if isinstance(current_meta, dict) and current_meta.get("ingest_key") == key:
                    _restore_deleted_receipt(key, previous)
            except Exception as exc:
                log.warning("delete outcome uncertain; retaining ingest tombstone: %s", type(exc).__name__)
            raise
    return row if isinstance(row, dict) else None


def _storage_config() -> tuple:
    return (os.path.abspath(settings.STATE_DIR), settings.CHROMA_MODE, settings.CHROMA_HOST,
            settings.CHROMA_PORT, settings.MEM0_COLLECTION_NAME, settings.MEM0_EMBEDDER_PROVIDER,
            settings.MEM0_EMBEDDER_MODEL, getattr(settings, "MEM0_HISTORY_DB_PATH", ""))


def _build_config() -> dict:
    """Build mem0 configuration from settings.

    CHROMA_MODE=server (default): connect to a Chroma HTTP server at
    CHROMA_HOST:CHROMA_PORT. This avoids the multi-process write race that
    can corrupt an embedded HNSW index under concurrent writers.

    CHROMA_MODE=embedded: direct on-disk access under STATE_DIR (single
    process only).
    """
    if settings.CHROMA_MODE not in {"server", "embedded"}:
        raise ValueError("CHROMA_MODE must be server or embedded")
    if settings.CHROMA_MODE == "server":
        vector_cfg = {
            "collection_name": settings.MEM0_COLLECTION_NAME,
            "host": settings.CHROMA_HOST,
            "port": settings.CHROMA_PORT,
        }
    else:
        db_path = os.path.join(settings.STATE_DIR, "mem0_db")
        os.makedirs(db_path, exist_ok=True)
        vector_cfg = {
            "collection_name": settings.MEM0_COLLECTION_NAME,
            "path": db_path,
        }

    backend = get_auth_backend()
    llm_model = settings.MEM0_LLM_MODEL or backend.default_model()

    cfg = {
        "llm": backend.mem0_llm_config(model=llm_model),
        "vector_store": {
            "provider": "chroma",
            "config": vector_cfg,
        },
        "embedder": {
            "provider": settings.MEM0_EMBEDDER_PROVIDER,
            "config": {
                "model": settings.MEM0_EMBEDDER_MODEL,
            },
        },
        "version": "v1.1",
    }
    history_path = getattr(settings, "MEM0_HISTORY_DB_PATH", "")
    if history_path:
        history_path = os.path.abspath(os.path.expanduser(history_path))
        os.makedirs(os.path.dirname(history_path), exist_ok=True)
        cfg["history_db_path"] = history_path
    return cfg


def _ping_chromadb_if_server_mode() -> None:
    """When CHROMA_MODE=server, verify the ChromaDB HTTP server is reachable.

    Raises an exception on failure so the caller can mark init as failed
    and trigger the cooldown-based retry path.
    """
    if settings.CHROMA_MODE != "server":
        return
    import requests
    r = requests.get(
        f"http://{settings.CHROMA_HOST}:{settings.CHROMA_PORT}/api/v2/heartbeat",
        timeout=5,
    )
    r.raise_for_status()


def _sync_backend_state(mem) -> None:
    """Give the auth backend a chance to sync rotated credentials onto the
    memory store's internal LLM client. Best-effort — a hook failure must
    never take down store access."""
    try:
        get_auth_backend().refresh_memory_llm(mem)
    except Exception as exc:
        log.warning("auth backend refresh_memory_llm failed (non-fatal): %s", exc)


def _provider_drifted() -> bool:
    """True when the auth backend's effective provider changed since the
    cached instance was built (e.g. OAuth session lost → API-key fallback,
    or restored). Best-effort: an erroring hook never forces a rebuild."""
    try:
        return get_auth_backend().provider_id() != _mem0_provider_id
    except Exception:
        return False


def _get_mem0():
    """Lazy-init singleton mem0 Memory instance with cooldown-based retry.

    On init failure, sets _init_failed_at and returns None for the next
    _INIT_RETRY_COOLDOWN_SEC seconds before retrying, so a transient
    ChromaDB outage recovers automatically instead of disabling the store
    for the rest of the process lifetime.

    Provider drift (``AuthBackend.provider_id`` changing between calls)
    rebuilds the instance so the store's internal LLM follows a runtime
    provider switch; ``AuthBackend.refresh_memory_llm`` runs on every
    hand-out so rotated OAuth tokens propagate to the cached instance.

    NOTE: first init does a synchronous ChromaDB heartbeat +
    Memory.from_config; call via ``asyncio.to_thread`` from event loops.
    """
    global _mem0_instance, _mem0_provider, _mem0_provider_id, _init_failed_at, _mem0_storage_config

    if (_mem0_instance is not None and _mem0_storage_config is not None
            and _mem0_storage_config != _storage_config()):
        log.error("mem0 storage/embedder configuration changed after initialization; restart required")
        return None

    if _mem0_instance is not None and not _provider_drifted():
        _sync_backend_state(_mem0_instance)
        return _mem0_instance

    with _init_lock:
        if _mem0_instance is not None:
            if not _provider_drifted():
                _sync_backend_state(_mem0_instance)
                return _mem0_instance
            log.info(
                "mem0 provider drift detected (%s -> %s) — rebuilding instance",
                _mem0_provider_id, get_auth_backend().provider_id(),
            )
            _mem0_instance = None
        if (
            _init_failed_at is not None
            and (time.monotonic() - _init_failed_at) < _INIT_RETRY_COOLDOWN_SEC
        ):
            return None
        try:
            _ping_chromadb_if_server_mode()
            from mem0 import Memory

            cfg = _build_config()
            _mem0_instance = Memory.from_config(cfg)
            _mem0_storage_config = _storage_config()
            _mem0_provider = cfg["llm"]["provider"]
            try:
                _mem0_provider_id = get_auth_backend().provider_id()
            except Exception:
                _mem0_provider_id = None
            _init_failed_at = None
            _sync_backend_state(_mem0_instance)
            vs_cfg = cfg["vector_store"]["config"]
            store_id = vs_cfg.get("path") or f'{vs_cfg.get("host")}:{vs_cfg.get("port")}'
            log.info(
                "mem0 ready — llm=%s provider=%s embedder=%s store=chroma(%s)",
                cfg["llm"]["config"]["model"],
                cfg["llm"]["provider"],
                cfg["embedder"]["config"]["model"],
                store_id,
            )
        except Exception as exc:
            log.error("mem0 init failed: %s", exc, exc_info=True)
            _mem0_instance = None
            _init_failed_at = time.monotonic()

    return _mem0_instance


# ---------------------------------------------------------------------------
# mem0ai API-generation compat
# ---------------------------------------------------------------------------
# mem0ai changed its retrieval API around 1.1: `user_id` moved from a
# top-level kwarg into ``filters={"user_id": ...}`` for search()/get_all()
# (add() still accepts the kwarg). We support both generations without
# pinning: try the legacy call first, and when the SDK rejects it switch
# styles for the rest of the process.
_filters_style: bool | None = None  # None=unknown, False=legacy kwarg, True=filters dict


def _rejects_user_id_kwarg(exc: Exception) -> bool:
    return "Top-level entity parameters" in str(exc)


def mem_search(mem, *, query: str, user_id: str, limit: int):
    """``mem.search`` across mem0ai API generations. Sync — wrap in to_thread."""
    global _filters_style
    if _filters_style:
        return mem.search(query=query, filters={"user_id": user_id}, limit=limit)
    try:
        result = mem.search(query=query, user_id=user_id, limit=limit)
        _filters_style = False
        return result
    except Exception as exc:
        if not _rejects_user_id_kwarg(exc):
            raise
        log.info("mem0 SDK uses filters-style params; switching")
        _filters_style = True
        return mem.search(query=query, filters={"user_id": user_id}, limit=limit)


def mem_get_all(mem, *, user_id: str, limit: int | None = None):
    """``mem.get_all`` across mem0ai API generations. Sync — wrap in to_thread."""
    global _filters_style
    # Chroma supports None as an unbounded query. Omitting the argument
    # instead silently applies the SDK's default (100), losing older facts.
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer or None")
    kwargs: dict = {"limit": limit}
    if _filters_style:
        return mem.get_all(filters={"user_id": user_id}, **kwargs)
    try:
        result = mem.get_all(user_id=user_id, **kwargs)
        _filters_style = False
        return result
    except Exception as exc:
        if not _rejects_user_id_kwarg(exc):
            raise
        log.info("mem0 SDK uses filters-style params; switching")
        _filters_style = True
        return mem.get_all(filters={"user_id": user_id}, **kwargs)


# ---------------------------------------------------------------------------
# Batch get — one Chroma round trip for a set of ids (explore-C #4)
# ---------------------------------------------------------------------------
# ``mem.get(id)`` is one Chroma ``collection.get(ids=[id])`` HTTP round trip.
# The retrieval hot path (search.py hydrate / graph sibling fetch) fans those
# out N-wide (``asyncio.gather`` + Semaphore), so a recall turn issues up to
# ~300 of them. ChromaDB's ``collection.get`` natively accepts a *list* of
# ids, so the whole fan-out collapses into a single round trip.
#
# Output invariance (Lean ``KanaeVerify.ChromaBatchGet``): for a distinct id
# list the batch returns the *same set*, in request order when projected by the
# caller, drops missing ids identically, and never adds a key outside the
# request. The per-record dict is byte-identical to ``mem.get(id)`` — same
# ``MemoryItem`` shaping (``score``/``metadata`` keys), same promoted payload
# keys, same timestamp normalisation — so downstream ``_is_fact_valid`` /
# dict-spread behaviour is unchanged.

_BATCH_PROMOTED_PAYLOAD_KEYS = ("user_id", "agent_id", "run_id", "actor_id", "role")
_BATCH_CORE_AND_PROMOTED_KEYS = {
    "data", "hash", "created_at", "updated_at", "id", *_BATCH_PROMOTED_PAYLOAD_KEYS,
}


def _format_vector_record(record_id: str, payload: dict | None) -> dict:
    """Shape one Chroma record into the exact dict ``Memory.get(id)`` returns.

    Mirrors ``mem0.memory.main.Memory.get`` line-for-line: ``MemoryItem``
    model dump (keeps the ``score``/``metadata`` keys), promoted payload keys,
    then the remaining payload fields collected under ``metadata``.
    """
    from mem0.configs.base import MemoryItem
    from mem0.memory.main import _normalize_iso_timestamp_to_utc

    payload = payload or {}
    item = MemoryItem(
        id=record_id,
        memory=payload.get("data", ""),
        hash=payload.get("hash"),
        created_at=_normalize_iso_timestamp_to_utc(payload.get("created_at")),
        updated_at=_normalize_iso_timestamp_to_utc(payload.get("updated_at")),
    ).model_dump()
    for key in _BATCH_PROMOTED_PAYLOAD_KEYS:
        if key in payload:
            item[key] = payload[key]
    extra = {k: v for k, v in payload.items() if k not in _BATCH_CORE_AND_PROMOTED_KEYS}
    if extra:
        item["metadata"] = extra
    return item


def _per_id_get_map(mem, ids) -> dict:
    """Fallback: the original N+1, returned as an id→fact map (misses dropped)."""
    out: dict = {}
    for i in ids:
        try:
            fact = mem.get(i)
        except Exception:
            fact = None
        if fact:
            out[i] = fact
    return out


def batch_get_facts_sync(mem, ids) -> dict:
    """One-round-trip batch equivalent of ``{i: mem.get(i)}`` for present ids.

    Returns a mapping ``{id: fact}`` containing only ids that exist; callers
    project it back in their own request order (so ordering is their loop's
    responsibility, exactly as with the per-id version). The request is
    de-duplicated and non-str/empty ids are dropped before the round trip.

    Falls back to per-id ``mem.get`` when the backing store is not a
    Chroma-style ``collection`` or the batch call raises — never returning
    fewer facts than the per-id loop would. Sync; wrap in ``to_thread``.
    """
    seen: set = set()
    uniq: list = []
    for i in ids:
        if isinstance(i, str) and i and i not in seen:
            seen.add(i)
            uniq.append(i)
    if not uniq:
        return {}

    vs = getattr(mem, "vector_store", None)
    coll = getattr(vs, "collection", None)
    coll_get = getattr(coll, "get", None)
    if coll is None or not callable(coll_get):
        return _per_id_get_map(mem, uniq)

    try:
        raw = coll_get(ids=uniq)  # single Chroma round trip
    except Exception as exc:
        log.warning("batch get failed (%s); falling back to per-id", type(exc).__name__)
        return _per_id_get_map(mem, uniq)

    # A real Chroma get returns {"ids": [...], "metadatas": [...], ...} with list
    # values. Anything else (None, malformed, a test double) → fall back to the
    # per-id path so we are never *less* correct than the original loop.
    if not isinstance(raw, dict):
        return _per_id_get_map(mem, uniq)
    got_ids = raw.get("ids")
    metadatas = raw.get("metadatas")
    if not isinstance(got_ids, list):
        return _per_id_get_map(mem, uniq)
    if not isinstance(metadatas, list):
        metadatas = []
    # chromadb's query path nests one level ([[...]]); the get path is flat.
    # Normalise both, same as mem0's ChromaDB._parse_output.
    if got_ids and isinstance(got_ids[0], list):
        got_ids = got_ids[0]
    if metadatas and isinstance(metadatas[0], list):
        metadatas = metadatas[0]

    out: dict = {}
    for idx, rid in enumerate(got_ids):
        payload = metadatas[idx] if idx < len(metadatas) else None
        out[rid] = _format_vector_record(rid, payload)
    return out


# ---------------------------------------------------------------------------
# Low-level housekeeping (direct mem0 access — used by housekeeping/compaction
# scripts and by search.py's graph-expansion path)
# ---------------------------------------------------------------------------

async def get_all_memories(
    user_id: str, limit: int | None = None, *, strict: bool = False,
) -> list[dict]:
    """Retrieve memories for a user.

    `limit` is forwarded to mem0 / ChromaDB so the underlying query can stop
    early instead of fetching the entire collection and slicing in Python.
    Pass None (default) to fetch all (fine for occasional housekeeping;
    avoid on hot paths).
    """
    mem = await asyncio.to_thread(_get_mem0)
    if not mem:
        if strict:
            raise RuntimeError("memory store unavailable")
        return []
    try:
        raw = await asyncio.to_thread(mem_get_all, mem, user_id=user_id, limit=limit)
        rows = raw.get("results") if isinstance(raw, dict) else raw
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            if strict:
                raise RuntimeError("malformed memory enumeration")
            return []
        return rows
    except Exception as exc:
        if strict:
            raise
        log.warning("mem0 get_all error: %s", exc)
        return []


def _delete_memory_sync(mem, memory_id: str) -> bool:
    from . import bm25_index, fact_store, graph, lineage
    from .reinforce import memory_mutation_lock
    from .search import invalidate_core_cache

    with memory_mutation_lock(memory_id):
        row = _delete_primary_with_receipt(mem, memory_id)
        metadata = (row.get("metadata") or {}) if isinstance(row, dict) else {}
        owner = (row.get("user_id") or metadata.get("user_id") or "") if row else ""
        repaired = True
        try:
            lineage.retire_source(memory_id, user_id=str(owner))
        except Exception as exc:
            repaired = False
            log.warning("memory lineage repair incomplete: %s", type(exc).__name__)
        # Each mirror is independent; one failed cleanup must not skip the rest.
        for cleanup in (graph.delete_fact_edges, graph.expire_relations_for_fact,
                        bm25_index.remove_fact, fact_store.remove_fact):
            try:
                cleanup(memory_id)
            except Exception as exc:
                log.warning("memory side-index cleanup failed: %s", exc)
        invalidate_core_cache()
    # Do not acquire dependent mutation locks while holding the source lock.
    # Cyclic/malformed legacy support must not deadlock concurrent deletions.
    try:
        result = lineage.repair_dependents_sync(mem, limit=100)
        repaired = repaired and not result.get("failed") and not result.get("pending")
    except Exception as exc:
        repaired = False
        log.warning("memory lineage repair incomplete: %s", type(exc).__name__)
    return repaired


async def delete_memory(memory_id: str) -> bool:
    """Delete a specific memory by ID."""
    mem = await asyncio.to_thread(_get_mem0)
    if not mem:
        return False
    try:
        # Drain the actual worker before cancellation is acknowledged. The
        # primary write, durable intent and repair must retain their lifetime
        # even when runtime shutdown cancels all asyncio Tasks at once.
        worker = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, _delete_memory_sync, mem, memory_id)
        cancelled = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception:
                break
        if cancelled is not None:
            try:
                worker.result()
            except Exception as exc:
                log.warning("mem0 delete failed during cancellation: %s", type(exc).__name__)
            raise cancelled
        return worker.result()
    except Exception as exc:
        log.warning("mem0 delete error: %s", exc)
        return False

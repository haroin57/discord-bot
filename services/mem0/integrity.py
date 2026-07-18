"""Canonical Mem0 ledger, transactional outbox, and durable work queues.

The SQLite ledger is the single source of truth when ``MEM0_INTEGRITY_MODE``
is ``shadow`` or ``canonical``. Chroma/Mem0, BM25, the entity graph, and the
structured fact store are rebuildable projections driven by an outbox written
in the same transaction as each canonical mutation.

Migration modes:

``legacy`` (default)
    Preserve the existing storage calls. Public application-service methods
    still provide one write boundary, but do not create ledger state.
``shadow``
    Write the canonical ledger first and synchronously drain its outbox. Reads
    may continue using legacy stores while coverage is backfilled.
``canonical``
    Same write path as shadow, and consumers may fail closed for rows missing
    from the canonical ledger.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

PROJECTIONS = ("vector", "bm25", "graph", "fact_store")
TERMINAL_STATUSES = ("acked", "dead")
PROJECTION_WATERMARK_CONSUMER = "mem0-projection"
DEFAULT_MAX_ATTEMPTS = int(os.environ.get("MEM0_OUTBOX_MAX_ATTEMPTS", "5"))
DEFAULT_LEASE_SECONDS = float(os.environ.get("MEM0_OUTBOX_LEASE_SECONDS", "30"))
DEFAULT_RETRY_SECONDS = float(os.environ.get("MEM0_OUTBOX_RETRY_SECONDS", "1"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_facts (
    fact_id       TEXT PRIMARY KEY,
    legacy_id     TEXT UNIQUE,
    user_id       TEXT NOT NULL,
    text          TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    entities_json TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'active',
    version       INTEGER NOT NULL DEFAULT 1,
    merged_into   TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canonical_user_status
    ON canonical_facts(user_id, status);

CREATE TABLE IF NOT EXISTS canonical_relations (
    relation_id TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    src_name    TEXT NOT NULL,
    rel_type    TEXT NOT NULL,
    dst_name    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE(user_id, src_name, rel_type, dst_name, status)
);
CREATE INDEX IF NOT EXISTS idx_canonical_relation_user
    ON canonical_relations(user_id, status);

CREATE TABLE IF NOT EXISTS outbox (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id           TEXT NOT NULL UNIQUE,
    aggregate_id       TEXT NOT NULL,
    projection         TEXT NOT NULL,
    operation          TEXT NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1,
    generation         INTEGER NOT NULL DEFAULT 0,
    payload_json       TEXT NOT NULL,
    depends_on_id      INTEGER,
    status             TEXT NOT NULL DEFAULT 'pending',
    attempts           INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL,
    available_at       REAL NOT NULL,
    lease_owner        TEXT,
    lease_token        TEXT,
    lease_expires_at   REAL,
    last_error         TEXT,
    created_at         REAL NOT NULL,
    acked_at           REAL,
    redrive_dead_letter_id INTEGER,
    FOREIGN KEY(depends_on_id) REFERENCES outbox(id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_ready
    ON outbox(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_outbox_aggregate
    ON outbox(aggregate_id, id);

CREATE TABLE IF NOT EXISTS processed_events (
    consumer    TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    processed_at REAL NOT NULL,
    PRIMARY KEY(consumer, event_id)
);

CREATE TABLE IF NOT EXISTS projection_cursors (
    consumer      TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projection_watermarks (
    consumer       TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    projection     TEXT NOT NULL,
    last_version   INTEGER NOT NULL,
    last_event_id  INTEGER NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY(consumer, aggregate_id, projection)
);

CREATE TABLE IF NOT EXISTS migration_coverage (
    provider_backend    TEXT NOT NULL,
    provider_endpoint   TEXT NOT NULL,
    provider_collection TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    provider_count  INTEGER NOT NULL,
    canonical_count INTEGER NOT NULL,
    provider_hash   TEXT NOT NULL,
    verified_at     REAL NOT NULL,
    PRIMARY KEY(provider_backend, provider_endpoint, provider_collection, user_id)
);

CREATE TABLE IF NOT EXISTS batch_accumulators (
    partition_key    TEXT PRIMARY KEY,
    cursor_value     INTEGER NOT NULL DEFAULT 0,
    turns_json       TEXT NOT NULL DEFAULT '[]',
    kanae_facts_json TEXT NOT NULL DEFAULT '[]',
    updated_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS work_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT NOT NULL UNIQUE,
    queue_name         TEXT NOT NULL,
    partition_key      TEXT NOT NULL,
    cursor_value       TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    attempts           INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL,
    available_at       REAL NOT NULL,
    lease_owner        TEXT,
    lease_token        TEXT,
    lease_expires_at   REAL,
    last_error         TEXT,
    created_at         REAL NOT NULL,
    acked_at           REAL,
    generation         INTEGER NOT NULL DEFAULT 0,
    redrive_dead_letter_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_work_ready
    ON work_items(queue_name, status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_work_partition
    ON work_items(queue_name, partition_key, id);

CREATE TABLE IF NOT EXISTS work_cursors (
    queue_name    TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    cursor_value TEXT NOT NULL,
    last_job_id  INTEGER NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY(queue_name, partition_key)
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind   TEXT NOT NULL,
    source_id     INTEGER NOT NULL,
    source_key    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    attempts      INTEGER NOT NULL,
    last_error    TEXT,
    failed_at     REAL NOT NULL,
    generation    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open',
    resolved_at   REAL,
    resolution_note TEXT,
    redrive_source_id INTEGER,
    UNIQUE(source_kind, source_id)
);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str, fallback):
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _default_db_path() -> Path:
    try:
        from config import STATE_DIR

        root = Path(STATE_DIR)
    except Exception:
        root = Path("/home/ubuntu/discord-bot/.state")
    return root / "mem0_canonical.sqlite"


@dataclass(frozen=True)
class Lease:
    id: int
    key: str
    payload: dict
    attempts: int
    owner: str
    projection: str = ""
    operation: str = ""
    aggregate_id: str = ""
    queue_name: str = ""
    partition_key: str = ""
    cursor_value: str = ""
    version: int = 0
    generation: int = 0
    token: str = ""


@dataclass(frozen=True)
class ProviderStorageIdentity:
    """Stable identity for the provider storage namespace being verified."""

    backend: str
    endpoint: str
    collection: str

    def cache_key(self) -> tuple[str, str, str]:
        return self.backend, self.endpoint, self.collection


class CanonicalRepository:
    """SQLite canonical fact repository with transactional outbox and queues."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path is not None else _default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        if not self._schema_ready:
            with self._schema_lock:
                if not self._schema_ready:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(_SCHEMA)
                    self._migrate_schema(conn)
                    conn.commit()
                    self._schema_ready = True
        return conn

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the first staged ledger deployment."""
        migrations = {
            "outbox": {
                "version": "INTEGER NOT NULL DEFAULT 1",
                "generation": "INTEGER NOT NULL DEFAULT 0",
                "redrive_dead_letter_id": "INTEGER",
                "lease_token": "TEXT",
            },
            "dead_letters": {
                "generation": "INTEGER NOT NULL DEFAULT 0",
                "status": "TEXT NOT NULL DEFAULT 'open'",
                "resolved_at": "REAL",
                "resolution_note": "TEXT",
                "redrive_source_id": "INTEGER",
            },
            "work_items": {
                "generation": "INTEGER NOT NULL DEFAULT 0",
                "redrive_dead_letter_id": "INTEGER",
                "lease_token": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            for column, declaration in columns.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}",
                    )
        coverage_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(migration_coverage)")
        }
        if "provider_backend" not in coverage_columns:
            conn.execute(
                "ALTER TABLE migration_coverage RENAME TO migration_coverage_v1"
            )
            conn.execute(
                """CREATE TABLE migration_coverage (
                       provider_backend TEXT NOT NULL,
                       provider_endpoint TEXT NOT NULL,
                       provider_collection TEXT NOT NULL,
                       user_id TEXT NOT NULL,
                       provider_count INTEGER NOT NULL,
                       canonical_count INTEGER NOT NULL,
                       provider_hash TEXT NOT NULL,
                       verified_at REAL NOT NULL,
                       PRIMARY KEY(
                           provider_backend, provider_endpoint,
                           provider_collection, user_id
                       )
                   )""",
            )
            conn.execute(
                """INSERT INTO migration_coverage
                       (provider_backend,provider_endpoint,provider_collection,
                        user_id,provider_count,canonical_count,provider_hash,verified_at)
                   SELECT '', '', '', user_id,provider_count,canonical_count,
                          provider_hash,verified_at FROM migration_coverage_v1""",
            )
            conn.execute("DROP TABLE migration_coverage_v1")

    @contextmanager
    def _tx(self):
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def _read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _fact_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = _loads(item.pop("metadata_json"), {})
        item["entities"] = _loads(item.pop("entities_json"), [])
        return item

    def get_fact(self, identifier: str) -> dict | None:
        if not identifier:
            return None
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM canonical_facts WHERE fact_id=? OR legacy_id=? LIMIT 1",
                (identifier, identifier),
            ).fetchone()
        return self._fact_dict(row)

    def list_facts(self, user_id: str, *, include_inactive: bool = False) -> list[dict]:
        sql = "SELECT * FROM canonical_facts WHERE user_id=?"
        params: tuple = (str(user_id),)
        if not include_inactive:
            sql += " AND status='active'"
        sql += " ORDER BY created_at, fact_id"
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._fact_dict(row) for row in rows]

    def active_facts_by_attribute(self, user_id: str, attribute: str) -> list[dict]:
        return [
            fact
            for fact in self.list_facts(user_id)
            if str((fact.get("metadata") or {}).get("attribute") or "") == attribute
        ]

    def _insert_outbox(
        self,
        conn: sqlite3.Connection,
        *,
        aggregate_id: str,
        version: int,
        projection: str,
        operation: str,
        payload: dict,
        depends_on_id: int | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> int:
        now = time.time()
        event_id = f"{aggregate_id}:{version}:{operation}:{projection}"
        cur = conn.execute(
            """INSERT OR IGNORE INTO outbox
               (event_id, aggregate_id, projection, operation, version, payload_json,
                depends_on_id, max_attempts, available_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                aggregate_id,
                projection,
                operation,
                version,
                _json(payload),
                depends_on_id,
                max_attempts,
                now,
                now,
            ),
        )
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = conn.execute(
            "SELECT id FROM outbox WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return int(row["id"])

    def _enqueue_fact_projections(
        self,
        conn: sqlite3.Connection,
        fact: dict,
        operation: str,
        *,
        include_vector: bool = True,
    ) -> None:
        payload = {
            "fact_id": fact["fact_id"],
            "legacy_id": fact.get("legacy_id"),
            "user_id": fact["user_id"],
            "text": fact["text"],
            "metadata": fact["metadata"],
            "entities": fact.get("entities") or [],
            "status": fact["status"],
            "merged_into": fact.get("merged_into"),
            "version": fact["version"],
        }
        vector_id = None
        if include_vector:
            vector_id = self._insert_outbox(
                conn,
                aggregate_id=fact["fact_id"],
                version=fact["version"],
                projection="vector",
                operation=operation,
                payload=payload,
            )
        for projection in ("bm25", "graph", "fact_store"):
            self._insert_outbox(
                conn,
                aggregate_id=fact["fact_id"],
                version=fact["version"],
                projection=projection,
                operation=operation,
                payload=payload,
                depends_on_id=vector_id if operation == "upsert" else None,
            )

    def create_fact(
        self,
        user_id: str,
        text: str,
        metadata: dict | None = None,
        *,
        entities: list[dict] | None = None,
        fact_id: str | None = None,
        legacy_id: str | None = None,
        project_vector: bool = True,
    ) -> dict:
        """Atomically create the canonical fact and all projection intents."""
        if not user_id or not text:
            raise ValueError("user_id and text are required")
        fact_id = fact_id or uuid.uuid4().hex
        now = time.time()
        meta = dict(metadata or {})
        meta.setdefault("canonical_id", fact_id)
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO canonical_facts
                   (fact_id, legacy_id, user_id, text, metadata_json,
                    entities_json, status, version, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,'active',1,?,?)""",
                (
                    fact_id,
                    legacy_id,
                    str(user_id),
                    text,
                    _json(meta),
                    _json(entities or []),
                    now,
                    now,
                ),
            )
            fact = {
                "fact_id": fact_id,
                "legacy_id": legacy_id,
                "user_id": str(user_id),
                "text": text,
                "metadata": meta,
                "entities": list(entities or []),
                "status": "active",
                "version": 1,
                "merged_into": None,
            }
            self._enqueue_fact_projections(
                conn,
                fact,
                "upsert",
                include_vector=project_vector,
            )
        return self.get_fact(fact_id)

    def adopt_legacy_fact(self, record: dict, *, user_id: str | None = None) -> dict:
        """Idempotently import an existing provider row for staged migration."""
        legacy_id = str(record.get("id") or record.get("memory_id") or "")
        text = str(record.get("memory") or record.get("text") or "")
        metadata = record.get("metadata") or {}
        uid = str(
            user_id or record.get("user_id") or metadata.get("user_id") or "legacy"
        )
        canonical_id = str(metadata.get("canonical_id") or legacy_id)
        if not legacy_id or not text:
            raise ValueError("legacy record requires id and text")
        existing = self.get_fact(legacy_id)
        if existing:
            return existing
        try:
            return self.create_fact(
                uid,
                text,
                metadata,
                fact_id=canonical_id,
                legacy_id=legacy_id,
                project_vector=False,
            )
        except sqlite3.IntegrityError:
            found = self.get_fact(legacy_id)
            if found is None:
                raise
            return found

    def bind_legacy_id(self, fact_id: str, legacy_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE canonical_facts SET legacy_id=?, updated_at=? WHERE fact_id=?",
                (legacy_id, time.time(), fact_id),
            )

    def _transition(
        self,
        identifier: str,
        *,
        status: str | None = None,
        metadata_patch: dict | None = None,
        merged_into: str | None = None,
        operation: str,
        projections: Iterable[str] | None = None,
    ) -> dict:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM canonical_facts WHERE fact_id=? OR legacy_id=? LIMIT 1",
                (identifier, identifier),
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            fact = self._fact_dict(row)
            meta = dict(fact["metadata"])
            meta.update(metadata_patch or {})
            version = int(fact["version"]) + 1
            next_status = status or fact["status"]
            conn.execute(
                """UPDATE canonical_facts SET metadata_json=?, status=?, version=?,
                   merged_into=?, updated_at=? WHERE fact_id=?""",
                (
                    _json(meta),
                    next_status,
                    version,
                    merged_into if merged_into is not None else fact.get("merged_into"),
                    time.time(),
                    fact["fact_id"],
                ),
            )
            fact.update(
                {
                    "metadata": meta,
                    "status": next_status,
                    "version": version,
                    "merged_into": merged_into
                    if merged_into is not None
                    else fact.get("merged_into"),
                }
            )
            wanted = tuple(projections or PROJECTIONS)
            payload = {
                "fact_id": fact["fact_id"],
                "user_id": fact["user_id"],
                "legacy_id": fact.get("legacy_id"),
                "text": fact["text"],
                "metadata": fact["metadata"],
                "metadata_patch": dict(metadata_patch or {}),
                "entities": fact.get("entities") or [],
                "status": fact["status"],
                "merged_into": fact.get("merged_into"),
                "version": version,
            }
            for projection in wanted:
                self._insert_outbox(
                    conn,
                    aggregate_id=fact["fact_id"],
                    version=version,
                    projection=projection,
                    operation=operation,
                    payload=payload,
                )
        return self.get_fact(fact["fact_id"])

    def archive_fact(
        self,
        identifier: str,
        *,
        reason: str,
        merged_into: str | None = None,
        at: int | None = None,
    ) -> dict:
        patch = {"valid_to": int(at or time.time()), "archive_reason": reason}
        if merged_into:
            patch["consolidated_into"] = merged_into
        return self._transition(
            identifier,
            status="archived",
            metadata_patch=patch,
            merged_into=merged_into,
            operation="archive",
        )

    def delete_fact(self, identifier: str, *, reason: str = "delete") -> dict:
        return self._transition(
            identifier,
            status="deleted",
            metadata_patch={"deleted_at": int(time.time()), "delete_reason": reason},
            operation="delete",
        )

    def patch_metadata(
        self,
        identifier: str,
        patch: dict,
        *,
        operation: str = "update",
        projections: Iterable[str] = ("vector",),
    ) -> dict:
        return self._transition(
            identifier,
            metadata_patch=patch,
            operation=operation,
            projections=projections,
        )

    def consolidate(
        self,
        user_id: str,
        source_ids: list[str],
        outputs: list[dict],
        *,
        reason: str = "consolidated",
    ) -> list[dict]:
        """Create replacements and archive sources in one local transaction."""
        if not source_ids or not outputs:
            return []
        now = time.time()
        created_ids: list[str] = []
        with self._tx() as conn:
            created: list[dict] = []
            for output in outputs:
                fact_id = str(output.get("fact_id") or uuid.uuid4().hex)
                text = str(output.get("text") or "")
                if not text:
                    raise ValueError("consolidated output text is required")
                meta = dict(output.get("metadata") or {})
                meta.setdefault("canonical_id", fact_id)
                meta.setdefault("source", "consolidate")
                meta.setdefault("consolidated_from", ",".join(source_ids))
                entities = list(output.get("entities") or [])
                conn.execute(
                    """INSERT INTO canonical_facts
                       (fact_id,user_id,text,metadata_json,entities_json,status,
                        version,created_at,updated_at)
                       VALUES (?,?,?,?,?,'active',1,?,?)""",
                    (
                        fact_id,
                        str(user_id),
                        text,
                        _json(meta),
                        _json(entities),
                        now,
                        now,
                    ),
                )
                fact = {
                    "fact_id": fact_id,
                    "legacy_id": None,
                    "user_id": str(user_id),
                    "text": text,
                    "metadata": meta,
                    "entities": entities,
                    "status": "active",
                    "version": 1,
                    "merged_into": None,
                }
                self._enqueue_fact_projections(conn, fact, "upsert")
                created.append(fact)
                created_ids.append(fact_id)

            merged_into = ",".join(created_ids)
            for source_id in source_ids:
                row = conn.execute(
                    "SELECT * FROM canonical_facts WHERE fact_id=? OR legacy_id=? LIMIT 1",
                    (source_id, source_id),
                ).fetchone()
                if row is None:
                    raise KeyError(source_id)
                fact = self._fact_dict(row)
                if fact["user_id"] != str(user_id):
                    raise ValueError(
                        f"source fact belongs to another user: {source_id}",
                    )
                meta = dict(fact["metadata"])
                meta.update(
                    {
                        "valid_to": int(now),
                        "archive_reason": reason,
                        "consolidated_into": merged_into,
                    }
                )
                version = int(fact["version"]) + 1
                conn.execute(
                    """UPDATE canonical_facts SET metadata_json=?,status='archived',
                       version=?,merged_into=?,updated_at=? WHERE fact_id=?""",
                    (_json(meta), version, merged_into, now, fact["fact_id"]),
                )
                fact.update(
                    {
                        "metadata": meta,
                        "status": "archived",
                        "version": version,
                        "merged_into": merged_into,
                    }
                )
                self._enqueue_fact_projections(conn, fact, "archive")
        return [self.get_fact(fid) for fid in created_ids]

    def create_relations(self, user_id: str, relations: Iterable[dict]) -> list[dict]:
        """Atomically persist canonical relations and graph projection intents."""
        uid = str(user_id)
        now = time.time()
        created: list[dict] = []
        with self._tx() as conn:
            for relation in relations:
                src_name = str(relation.get("src") or "").strip()
                rel_type = str(relation.get("rel_type") or "").strip()
                dst_name = str(relation.get("dst") or "").strip()
                if not (uid and src_name and rel_type and dst_name):
                    continue
                raw_key = f"{uid}\0{src_name}\0{rel_type}\0{dst_name}"
                relation_id = hashlib.sha256(raw_key.encode()).hexdigest()
                cur = conn.execute(
                    """INSERT OR IGNORE INTO canonical_relations
                       (relation_id,user_id,src_name,rel_type,dst_name,
                        status,version,created_at,updated_at)
                       VALUES (?,?,?,?,?,'active',1,?,?)""",
                    (relation_id, uid, src_name, rel_type, dst_name, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM canonical_relations WHERE relation_id=?",
                    (relation_id,),
                ).fetchone()
                item = dict(row)
                if not cur.rowcount:
                    continue
                created.append(item)
                self._insert_outbox(
                    conn,
                    aggregate_id=f"relation:{relation_id}",
                    version=1,
                    projection="graph",
                    operation="relation_upsert",
                    payload=item,
                )
        return created

    def list_relations(self, user_id: str) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT * FROM canonical_relations
                   WHERE user_id=? AND status='active' ORDER BY created_at,relation_id""",
                (str(user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- durable outbox -------------------------------------------------
    def _discard_stale_outbox(
        self,
        conn: sqlite3.Connection,
        consumer: str,
    ) -> None:
        rows = conn.execute(
            """SELECT o.id,o.event_id,o.version,o.redrive_dead_letter_id
               FROM outbox o
               JOIN projection_watermarks w
                 ON w.consumer=? AND w.aggregate_id=o.aggregate_id
                AND w.projection=o.projection
               WHERE o.status='pending' AND o.version<=w.last_version""",
            (consumer,),
        ).fetchall()
        now = time.time()
        for row in rows:
            conn.execute(
                """UPDATE outbox SET status='acked',acked_at=?,lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,
                   last_error='stale version rejected'
                   WHERE id=? AND status='pending'""",
                (now, row["id"]),
            )
            conn.execute(
                """INSERT OR IGNORE INTO processed_events
                   (consumer,event_id,processed_at) VALUES (?,?,?)""",
                (consumer, row["event_id"], now),
            )
            if row["redrive_dead_letter_id"]:
                conn.execute(
                    """UPDATE dead_letters SET status='resolved',resolved_at=?,
                       resolution_note='redrive superseded by newer watermark'
                       WHERE id=?""",
                    (now, row["redrive_dead_letter_id"]),
                )

    def lease_outbox(
        self,
        owner: str,
        *,
        limit: int = 20,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        aggregate_id: str | None = None,
    ) -> list[Lease]:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """UPDATE outbox SET status='pending',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL
                   WHERE status='leased' AND lease_expires_at<=?""",
                (now,),
            )
            self._discard_stale_outbox(conn, PROJECTION_WATERMARK_CONSUMER)
            self._advance_projection_cursor(conn, PROJECTION_WATERMARK_CONSUMER)
            where = [
                "o.status='pending'",
                "o.available_at<=?",
                "(o.depends_on_id IS NULL OR EXISTS ("
                "SELECT 1 FROM outbox d WHERE d.id=o.depends_on_id "
                "AND d.status='acked'))",
                "NOT EXISTS (SELECT 1 FROM outbox prior "
                "WHERE prior.aggregate_id=o.aggregate_id "
                "AND prior.projection=o.projection AND prior.id<o.id "
                "AND prior.status NOT IN ('acked','dead'))",
            ]
            params: list = [now]
            if aggregate_id is not None:
                where.append("o.aggregate_id=?")
                params.append(aggregate_id)
            params.append(max(1, int(limit)))
            rows = conn.execute(
                "SELECT o.* FROM outbox o WHERE "  # nosec B608
                + " AND ".join(where)
                + " ORDER BY o.id LIMIT ?",
                tuple(params),
            ).fetchall()
            leases: list[Lease] = []
            for row in rows:
                token = uuid.uuid4().hex
                conn.execute(
                    """UPDATE outbox SET status='leased',lease_owner=?,
                       lease_token=?,lease_expires_at=?,attempts=attempts+1
                       WHERE id=?""",
                    (owner, token, now + lease_seconds, row["id"]),
                )
                leases.append(
                    Lease(
                        id=int(row["id"]),
                        key=row["event_id"],
                        payload=_loads(row["payload_json"], {}),
                        attempts=int(row["attempts"]) + 1,
                        owner=owner,
                        projection=row["projection"],
                        operation=row["operation"],
                        aggregate_id=row["aggregate_id"],
                        version=int(row["version"]),
                        generation=int(row["generation"]),
                        token=token,
                    )
                )
        return leases

    def _advance_projection_cursor(
        self, conn: sqlite3.Connection, consumer: str
    ) -> None:
        row = conn.execute(
            "SELECT last_event_id FROM projection_cursors WHERE consumer=?",
            (consumer,),
        ).fetchone()
        cursor = int(row["last_event_id"]) if row else 0
        rows = conn.execute(
            "SELECT id,status FROM outbox WHERE id>? ORDER BY id",
            (cursor,),
        ).fetchall()
        for item in rows:
            if item["status"] not in TERMINAL_STATUSES:
                break
            cursor = int(item["id"])
        conn.execute(
            """INSERT INTO projection_cursors(consumer,last_event_id,updated_at)
               VALUES (?,?,?) ON CONFLICT(consumer) DO UPDATE SET
               last_event_id=excluded.last_event_id,updated_at=excluded.updated_at""",
            (consumer, cursor, time.time()),
        )

    def ack_outbox(self, lease: Lease, consumer: str) -> bool:
        with self._tx() as conn:
            now = time.time()
            cur = conn.execute(
                """UPDATE outbox SET status='acked',acked_at=?,lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,last_error=NULL
                   WHERE id=? AND status='leased' AND lease_owner=?
                   AND lease_token=? AND lease_expires_at>?""",
                (now, lease.id, lease.owner, lease.token, now),
            )
            if not cur.rowcount:
                return False
            conn.execute(
                "INSERT OR IGNORE INTO processed_events(consumer,event_id,processed_at) "
                "VALUES (?,?,?)",
                (consumer, lease.key, time.time()),
            )
            conn.execute(
                """INSERT INTO projection_watermarks
                   (consumer,aggregate_id,projection,last_version,last_event_id,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(consumer,aggregate_id,projection) DO UPDATE SET
                   last_version=MAX(last_version,excluded.last_version),
                   last_event_id=CASE
                       WHEN excluded.last_version>=last_version
                       THEN excluded.last_event_id ELSE last_event_id END,
                   updated_at=excluded.updated_at""",
                (
                    PROJECTION_WATERMARK_CONSUMER,
                    lease.aggregate_id,
                    lease.projection,
                    lease.version,
                    lease.id,
                    time.time(),
                ),
            )
            if lease.generation and lease.id:
                row = conn.execute(
                    "SELECT redrive_dead_letter_id FROM outbox WHERE id=?",
                    (lease.id,),
                ).fetchone()
                if row and row["redrive_dead_letter_id"]:
                    conn.execute(
                        """UPDATE dead_letters SET status='resolved',resolved_at=?,
                           resolution_note='redrive acknowledged'
                           WHERE id=?""",
                        (time.time(), row["redrive_dead_letter_id"]),
                    )
            self._advance_projection_cursor(conn, consumer)
        return True

    def retry_outbox(
        self,
        lease: Lease,
        error: BaseException | str,
        *,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> str:
        message = str(error)[:1000]
        with self._tx() as conn:
            now = time.time()
            row = conn.execute(
                """SELECT * FROM outbox WHERE id=? AND status='leased'
                   AND lease_owner=? AND lease_token=? AND lease_expires_at>?""",
                (lease.id, lease.owner, lease.token, now),
            ).fetchone()
            if row is None:
                return "lost"
            if int(row["attempts"]) >= int(row["max_attempts"]):
                status = "dead"
                conn.execute(
                    """UPDATE outbox SET status='dead',last_error=?,lease_owner=NULL,
                       lease_token=NULL,lease_expires_at=NULL WHERE id=?""",
                    (message, lease.id),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO dead_letters
                       (source_kind,source_id,source_key,payload_json,attempts,
                        last_error,failed_at,generation)
                       VALUES ('outbox',?,?,?,?,?,?,?)""",
                    (
                        lease.id,
                        lease.key,
                        row["payload_json"],
                        row["attempts"],
                        message,
                        time.time(),
                        row["generation"],
                    ),
                )
                if row["redrive_dead_letter_id"]:
                    conn.execute(
                        """UPDATE dead_letters SET status='resolved',resolved_at=?,
                           resolution_note=? WHERE id=?""",
                        (
                            time.time(),
                            f"redrive failed; next generation opened: {message}",
                            row["redrive_dead_letter_id"],
                        ),
                    )
                self._dead_letter_dependents(conn, lease.id, message)
                self._advance_projection_cursor(conn, PROJECTION_WATERMARK_CONSUMER)
            else:
                status = "pending"
                conn.execute(
                    """UPDATE outbox SET status='pending',last_error=?,available_at=?,
                       lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
                       WHERE id=?""",
                    (message, time.time() + max(0.0, retry_seconds), lease.id),
                )
        return status

    def _dead_letter_dependents(
        self,
        conn: sqlite3.Connection,
        source_id: int,
        error: str,
    ) -> None:
        """Move events blocked by a dead dependency into the dead-letter set."""
        pending = [source_id]
        while pending:
            dependency_id = pending.pop()
            rows = conn.execute(
                """SELECT * FROM outbox WHERE depends_on_id=?
                   AND status NOT IN ('acked','dead')""",
                (dependency_id,),
            ).fetchall()
            for row in rows:
                message = f"dependency {dependency_id} dead: {error}"[:1000]
                conn.execute(
                    """UPDATE outbox SET status='dead',last_error=?,lease_owner=NULL,
                       lease_token=NULL,lease_expires_at=NULL WHERE id=?""",
                    (message, row["id"]),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO dead_letters
                       (source_kind,source_id,source_key,payload_json,attempts,
                        last_error,failed_at) VALUES ('outbox',?,?,?,?,?,?)""",
                    (
                        row["id"],
                        row["event_id"],
                        row["payload_json"],
                        row["attempts"],
                        message,
                        time.time(),
                    ),
                )
                pending.append(int(row["id"]))

    def outbox_rows(self, *, aggregate_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM outbox"
        params: tuple = ()
        if aggregate_id is not None:
            sql += " WHERE aggregate_id=?"
            params = (aggregate_id,)
        sql += " ORDER BY id"
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def unfinished_outbox_count(self) -> int:
        """Return projection events that still require delivery or retry."""
        with self._read() as conn:
            row = conn.execute(
                "SELECT count(*) AS count FROM outbox "
                "WHERE status NOT IN ('acked','dead')",
            ).fetchone()
        return int(row["count"])

    def projection_cursor(self, consumer: str = "mem0-projection") -> int:
        with self._read() as conn:
            row = conn.execute(
                "SELECT last_event_id FROM projection_cursors WHERE consumer=?",
                (consumer,),
            ).fetchone()
        return int(row["last_event_id"]) if row else 0

    def projection_watermark(
        self,
        aggregate_id: str,
        projection: str,
    ) -> int:
        with self._read() as conn:
            row = conn.execute(
                """SELECT last_version FROM projection_watermarks
                   WHERE consumer=? AND aggregate_id=? AND projection=?""",
                (PROJECTION_WATERMARK_CONSUMER, aggregate_id, projection),
            ).fetchone()
        return int(row["last_version"]) if row else 0

    def renew_outbox(
        self,
        lease: Lease,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Extend a live outbox lease without reviving an expired claim."""
        with self._tx() as conn:
            now = time.time()
            cur = conn.execute(
                """UPDATE outbox SET lease_expires_at=?
                   WHERE id=? AND status='leased' AND lease_owner=?
                   AND lease_token=? AND lease_expires_at>?""",
                (
                    now + max(0.001, float(lease_seconds)),
                    lease.id,
                    lease.owner,
                    lease.token,
                    now,
                ),
            )
        return bool(cur.rowcount)

    def migration_coverage(
        self,
        user_id: str,
        provider_identity: ProviderStorageIdentity,
    ) -> dict | None:
        with self._read() as conn:
            row = conn.execute(
                """SELECT * FROM migration_coverage
                   WHERE provider_backend=? AND provider_endpoint=?
                   AND provider_collection=? AND user_id=?""",
                (*provider_identity.cache_key(), str(user_id)),
            ).fetchone()
        return dict(row) if row else None

    def migration_users(self) -> list[str]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT user_id FROM migration_coverage
                   UNION SELECT user_id FROM canonical_facts ORDER BY user_id""",
            ).fetchall()
        return [str(row["user_id"]) for row in rows]

    def record_migration_coverage(
        self,
        user_id: str,
        provider_ids: Iterable[str],
        *,
        provider_identity: ProviderStorageIdentity,
    ) -> dict:
        ids = sorted({str(item) for item in provider_ids if item})
        digest = hashlib.sha256("\0".join(ids).encode()).hexdigest()
        with self._tx() as conn:
            canonical_count = int(
                conn.execute(
                    "SELECT count(*) FROM canonical_facts WHERE user_id=?",
                    (str(user_id),),
                ).fetchone()[0]
            )
            incomplete = int(
                conn.execute(
                    """SELECT count(*) FROM outbox o JOIN canonical_facts f
                   ON f.fact_id=o.aggregate_id WHERE f.user_id=?
                   AND o.status!='acked'""",
                    (str(user_id),),
                ).fetchone()[0]
            )
            if incomplete:
                raise RuntimeError(
                    f"migration projections are incomplete for user {user_id}",
                )
            missing = [
                item
                for item in ids
                if conn.execute(
                    """SELECT 1 FROM canonical_facts
                       WHERE user_id=? AND (fact_id=? OR legacy_id=?) LIMIT 1""",
                    (str(user_id), item, item),
                ).fetchone()
                is None
            ]
            if missing:
                raise RuntimeError(
                    f"canonical migration coverage missing {len(missing)} provider rows",
                )
            provider_set = set(ids)
            missing_vectors = [
                row["legacy_id"]
                for row in conn.execute(
                    """SELECT legacy_id FROM canonical_facts
                       WHERE user_id=? AND status='active' AND legacy_id IS NOT NULL""",
                    (str(user_id),),
                ).fetchall()
                if row["legacy_id"] not in provider_set
            ]
            if missing_vectors:
                raise RuntimeError(
                    f"vector migration coverage missing {len(missing_vectors)} canonical rows",
                )
            conn.execute(
                """INSERT INTO migration_coverage
                   (provider_backend,provider_endpoint,provider_collection,user_id,
                    provider_count,canonical_count,provider_hash,verified_at)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(
                       provider_backend,provider_endpoint,provider_collection,user_id
                   ) DO UPDATE SET
                   provider_count=excluded.provider_count,
                   canonical_count=excluded.canonical_count,
                   provider_hash=excluded.provider_hash,
                   verified_at=excluded.verified_at""",
                (
                    *provider_identity.cache_key(),
                    str(user_id),
                    len(ids),
                    canonical_count,
                    digest,
                    time.time(),
                ),
            )
        return self.migration_coverage(str(user_id), provider_identity)

    def dead_letters(
        self,
        source_kind: str | None = None,
        *,
        status: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM dead_letters"
        where: list[str] = []
        params: list = []
        if source_kind:
            where.append("source_kind=?")
            params.append(source_kind)
        if status:
            where.append("status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        with self._read() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def resolve_dead_letter(self, dead_letter_id: int, note: str = "manual") -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                """UPDATE dead_letters SET status='resolved',resolved_at=?,
                   resolution_note=? WHERE id=? AND status!='resolved'""",
                (time.time(), str(note)[:1000], int(dead_letter_id)),
            )
        return bool(cur.rowcount)

    def redrive_dead_letter(self, dead_letter_id: int) -> int:
        """Create a generation-tagged retry and retain the original DLQ row."""
        now = time.time()
        with self._tx() as conn:
            dead = conn.execute(
                """SELECT * FROM dead_letters WHERE id=?
                   AND status='open'""",
                (int(dead_letter_id),),
            ).fetchone()
            if dead is None:
                raise ValueError("dead letter is not redrivable")
            generation = int(dead["generation"]) + 1
            if dead["source_kind"] == "outbox":
                source = conn.execute(
                    "SELECT * FROM outbox WHERE id=?",
                    (dead["source_id"],),
                ).fetchone()
                if source is None:
                    raise KeyError(dead["source_id"])
                event_id = (
                    f"{source['event_id']}:redrive:{generation}:{uuid.uuid4().hex}"
                )
                cur = conn.execute(
                    """INSERT INTO outbox
                       (event_id,aggregate_id,projection,operation,version,generation,
                        payload_json,max_attempts,available_at,created_at,
                        redrive_dead_letter_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        source["aggregate_id"],
                        source["projection"],
                        source["operation"],
                        source["version"],
                        generation,
                        source["payload_json"],
                        source["max_attempts"],
                        now,
                        now,
                        dead["id"],
                    ),
                )
            elif dead["source_kind"] == "work":
                source = conn.execute(
                    "SELECT * FROM work_items WHERE id=?",
                    (dead["source_id"],),
                ).fetchone()
                if source is None:
                    raise KeyError(dead["source_id"])
                job_id = f"{source['job_id']}:redrive:{generation}:{uuid.uuid4().hex}"
                cur = conn.execute(
                    """INSERT INTO work_items
                       (job_id,queue_name,partition_key,cursor_value,payload_json,
                        max_attempts,available_at,created_at,generation,
                        redrive_dead_letter_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        source["queue_name"],
                        source["partition_key"],
                        source["cursor_value"],
                        source["payload_json"],
                        source["max_attempts"],
                        now,
                        now,
                        generation,
                        dead["id"],
                    ),
                )
            else:
                raise ValueError(f"unsupported dead-letter kind: {dead['source_kind']}")
            redrive_id = int(cur.lastrowid)
            conn.execute(
                """UPDATE dead_letters SET status='redriven',redrive_source_id=?,
                   resolution_note=? WHERE id=?""",
                (redrive_id, f"generation {generation} scheduled", dead["id"]),
            )
        return redrive_id

    # ---- durable batch/replay queue ------------------------------------
    def append_batch_turn(self, partition_key: str, turn: dict) -> int:
        partition = str(partition_key)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM batch_accumulators WHERE partition_key=?",
                (partition,),
            ).fetchone()
            turns = _loads(row["turns_json"], []) if row else []
            facts = _loads(row["kanae_facts_json"], []) if row else []
            cursor = int(row["cursor_value"]) if row else 0
            idempotency_key = turn.get("idempotency_key")
            if idempotency_key:
                claimed = conn.execute(
                    """INSERT INTO processed_events(consumer,event_id,processed_at)
                       VALUES(?,?,?) ON CONFLICT(consumer,event_id) DO NOTHING""",
                    (
                        "bot-turn-record",
                        f"{partition}:{idempotency_key}",
                        time.time(),
                    ),
                )
                if claimed.rowcount == 0:
                    return len(turns)
            turns.append(dict(turn))
            conn.execute(
                """INSERT INTO batch_accumulators
                   (partition_key,cursor_value,turns_json,kanae_facts_json,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(partition_key) DO UPDATE SET
                   cursor_value=excluded.cursor_value,turns_json=excluded.turns_json,
                   kanae_facts_json=excluded.kanae_facts_json,
                   updated_at=excluded.updated_at""",
                (partition, cursor + 1, _json(turns), _json(facts), time.time()),
            )
        return len(turns)

    def append_batch_facts(self, partition_key: str, facts: Iterable[str]) -> int:
        partition = str(partition_key)
        incoming = [str(item) for item in facts]
        if not incoming:
            return 0
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM batch_accumulators WHERE partition_key=?",
                (partition,),
            ).fetchone()
            turns = _loads(row["turns_json"], []) if row else []
            existing = _loads(row["kanae_facts_json"], []) if row else []
            cursor = int(row["cursor_value"]) if row else 0
            existing.extend(incoming)
            conn.execute(
                """INSERT INTO batch_accumulators
                   (partition_key,cursor_value,turns_json,kanae_facts_json,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(partition_key) DO UPDATE SET
                   cursor_value=excluded.cursor_value,turns_json=excluded.turns_json,
                   kanae_facts_json=excluded.kanae_facts_json,
                   updated_at=excluded.updated_at""",
                (partition, cursor + 1, _json(turns), _json(existing), time.time()),
            )
        return len(existing)

    def finalize_and_lease_batch(
        self,
        partition_key: str,
        owner: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> Lease | None:
        """Finalize the accumulator and lease its work in one transaction."""
        partition = str(partition_key)
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """UPDATE work_items SET status='pending',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL WHERE queue_name='batch'
                   AND status='leased' AND lease_expires_at<=?""",
                (now,),
            )
            accumulator = conn.execute(
                "SELECT * FROM batch_accumulators WHERE partition_key=?",
                (partition,),
            ).fetchone()
            if accumulator:
                turns = _loads(accumulator["turns_json"], [])
                facts = _loads(accumulator["kanae_facts_json"], [])
                cursor = str(accumulator["cursor_value"])
                if turns or facts:
                    payload = {"turns": turns, "kanae_facts": facts}
                    raw_key = f"batch\0{partition}\0{cursor}\0{_json(payload)}"
                    job_id = hashlib.sha256(raw_key.encode()).hexdigest()
                    conn.execute(
                        """INSERT OR IGNORE INTO work_items
                           (job_id,queue_name,partition_key,cursor_value,payload_json,
                            max_attempts,available_at,created_at)
                           VALUES (?,'batch',?,?,?,?,?,?)""",
                        (
                            job_id,
                            partition,
                            cursor,
                            _json(payload),
                            max_attempts,
                            now,
                            now,
                        ),
                    )
                    conn.execute(
                        """UPDATE batch_accumulators SET turns_json='[]',
                           kanae_facts_json='[]',updated_at=? WHERE partition_key=?""",
                        (now, partition),
                    )
            row = conn.execute(
                """SELECT w.* FROM work_items w WHERE w.queue_name='batch'
                   AND w.partition_key=? AND w.status='pending' AND w.available_at<=?
                   AND NOT EXISTS (SELECT 1 FROM work_items e
                       WHERE e.queue_name=w.queue_name
                       AND e.partition_key=w.partition_key AND e.id<w.id
                       AND e.status NOT IN ('acked','dead'))
                   ORDER BY w.id LIMIT 1""",
                (partition, now),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            conn.execute(
                """UPDATE work_items SET status='leased',lease_owner=?,
                   lease_token=?,lease_expires_at=?,attempts=attempts+1 WHERE id=?""",
                (owner, token, now + lease_seconds, row["id"]),
            )
            return Lease(
                id=int(row["id"]),
                key=row["job_id"],
                payload=_loads(row["payload_json"], {}),
                attempts=int(row["attempts"]) + 1,
                owner=owner,
                queue_name="batch",
                partition_key=partition,
                cursor_value=row["cursor_value"],
                generation=int(row["generation"]),
                token=token,
            )

    def enqueue_work(
        self,
        queue_name: str,
        partition_key: str,
        cursor_value: str,
        payload: dict,
        *,
        job_id: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> str:
        raw_key = f"{queue_name}\0{partition_key}\0{cursor_value}\0{_json(payload)}"
        job_id = job_id or hashlib.sha256(raw_key.encode()).hexdigest()
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO work_items
                   (job_id,queue_name,partition_key,cursor_value,payload_json,
                    max_attempts,available_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    queue_name,
                    partition_key,
                    str(cursor_value),
                    _json(payload),
                    max_attempts,
                    now,
                    now,
                ),
            )
        return job_id

    def lease_work(
        self,
        queue_name: str,
        owner: str,
        *,
        limit: int = 1,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        partition_key: str | None = None,
    ) -> list[Lease]:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """UPDATE work_items SET status='pending',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL
                   WHERE queue_name=? AND status='leased'
                   AND lease_expires_at<=?""",
                (queue_name, now),
            )
            where = [
                "w.queue_name=?",
                "w.status='pending'",
                "w.available_at<=?",
                "NOT EXISTS (SELECT 1 FROM work_items e "
                "WHERE e.queue_name=w.queue_name AND e.partition_key=w.partition_key "
                "AND e.id<w.id AND e.status NOT IN ('acked','dead'))",
            ]
            params: list = [queue_name, now]
            if partition_key is not None:
                where.append("w.partition_key=?")
                params.append(str(partition_key))
            params.append(max(1, int(limit)))
            rows = conn.execute(
                "SELECT w.* FROM work_items w WHERE "  # nosec B608
                + " AND ".join(where)
                + " ORDER BY w.id LIMIT ?",
                tuple(params),
            ).fetchall()
            leases: list[Lease] = []
            for row in rows:
                token = uuid.uuid4().hex
                conn.execute(
                    """UPDATE work_items SET status='leased',lease_owner=?,
                       lease_token=?,lease_expires_at=?,attempts=attempts+1
                       WHERE id=?""",
                    (owner, token, now + lease_seconds, row["id"]),
                )
                leases.append(
                    Lease(
                        id=int(row["id"]),
                        key=row["job_id"],
                        payload=_loads(row["payload_json"], {}),
                        attempts=int(row["attempts"]) + 1,
                        owner=owner,
                        queue_name=row["queue_name"],
                        partition_key=row["partition_key"],
                        cursor_value=row["cursor_value"],
                        generation=int(row["generation"]),
                        token=token,
                    )
                )
        return leases

    def ack_work(self, lease: Lease) -> bool:
        with self._tx() as conn:
            now = time.time()
            cur = conn.execute(
                """UPDATE work_items SET status='acked',acked_at=?,lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,last_error=NULL
                   WHERE id=? AND status='leased' AND lease_owner=?
                   AND lease_token=? AND lease_expires_at>?""",
                (now, lease.id, lease.owner, lease.token, now),
            )
            if not cur.rowcount:
                return False
            if lease.generation == 0:
                conn.execute(
                    """INSERT INTO work_cursors
                       (queue_name,partition_key,cursor_value,last_job_id,updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(queue_name,partition_key) DO UPDATE SET
                       cursor_value=excluded.cursor_value,
                       last_job_id=excluded.last_job_id,
                       updated_at=excluded.updated_at
                       WHERE excluded.last_job_id>=work_cursors.last_job_id""",
                    (
                        lease.queue_name,
                        lease.partition_key,
                        lease.cursor_value,
                        lease.id,
                        time.time(),
                    ),
                )
            if lease.generation:
                row = conn.execute(
                    "SELECT redrive_dead_letter_id FROM work_items WHERE id=?",
                    (lease.id,),
                ).fetchone()
                if row and row["redrive_dead_letter_id"]:
                    conn.execute(
                        """UPDATE dead_letters SET status='resolved',resolved_at=?,
                           resolution_note='redrive acknowledged' WHERE id=?""",
                        (time.time(), row["redrive_dead_letter_id"]),
                    )
        return True

    def renew_work(
        self,
        lease: Lease,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Extend a live work lease without reviving an expired claim."""
        with self._tx() as conn:
            now = time.time()
            cur = conn.execute(
                """UPDATE work_items SET lease_expires_at=?
                   WHERE id=? AND status='leased' AND lease_owner=?
                   AND lease_token=? AND lease_expires_at>?""",
                (
                    now + max(0.001, float(lease_seconds)),
                    lease.id,
                    lease.owner,
                    lease.token,
                    now,
                ),
            )
        return bool(cur.rowcount)

    def retry_work(
        self,
        lease: Lease,
        error: BaseException | str,
        *,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> str:
        message = str(error)[:1000]
        with self._tx() as conn:
            now = time.time()
            row = conn.execute(
                """SELECT * FROM work_items WHERE id=? AND status='leased'
                   AND lease_owner=? AND lease_token=? AND lease_expires_at>?""",
                (lease.id, lease.owner, lease.token, now),
            ).fetchone()
            if row is None:
                return "lost"
            if int(row["attempts"]) >= int(row["max_attempts"]):
                status = "dead"
                conn.execute(
                    """UPDATE work_items SET status='dead',last_error=?,lease_owner=NULL,
                       lease_token=NULL,lease_expires_at=NULL WHERE id=?""",
                    (message, lease.id),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO dead_letters
                       (source_kind,source_id,source_key,payload_json,attempts,
                        last_error,failed_at,generation)
                       VALUES ('work',?,?,?,?,?,?,?)""",
                    (
                        lease.id,
                        lease.key,
                        row["payload_json"],
                        row["attempts"],
                        message,
                        time.time(),
                        row["generation"],
                    ),
                )
                if row["redrive_dead_letter_id"]:
                    conn.execute(
                        """UPDATE dead_letters SET status='resolved',resolved_at=?,
                           resolution_note=? WHERE id=?""",
                        (
                            time.time(),
                            f"redrive failed; next generation opened: {message}",
                            row["redrive_dead_letter_id"],
                        ),
                    )
                if lease.generation == 0:
                    conn.execute(
                        """INSERT INTO work_cursors
                           (queue_name,partition_key,cursor_value,last_job_id,updated_at)
                           VALUES (?,?,?,?,?)
                           ON CONFLICT(queue_name,partition_key) DO UPDATE SET
                           cursor_value=excluded.cursor_value,
                           last_job_id=excluded.last_job_id,
                           updated_at=excluded.updated_at
                           WHERE excluded.last_job_id>=work_cursors.last_job_id""",
                        (
                            lease.queue_name,
                            lease.partition_key,
                            lease.cursor_value,
                            lease.id,
                            time.time(),
                        ),
                    )
            else:
                status = "pending"
                conn.execute(
                    """UPDATE work_items SET status='pending',last_error=?,available_at=?,
                       lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
                       WHERE id=?""",
                    (message, time.time() + max(0.0, retry_seconds), lease.id),
                )
        return status

    def work_cursor(self, queue_name: str, partition_key: str) -> str:
        with self._read() as conn:
            row = conn.execute(
                """SELECT cursor_value FROM work_cursors
                   WHERE queue_name=? AND partition_key=?""",
                (queue_name, str(partition_key)),
            ).fetchone()
        return str(row["cursor_value"]) if row else ""

    def work_rows(self, queue_name: str) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM work_items WHERE queue_name=? ORDER BY id",
                (queue_name,),
            ).fetchall()
        return [dict(row) for row in rows]


class _OutboxLeaseHeartbeat:
    """Keep one projection claim live while its synchronous handler runs."""

    def __init__(
        self,
        repository: CanonicalRepository,
        lease: Lease,
        *,
        lease_seconds: float,
        interval_seconds: float,
    ):
        self.repository = repository
        self.lease = lease
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> bool:
        if not self.repository.renew_outbox(
            self.lease,
            lease_seconds=self.lease_seconds,
        ):
            self._lost.set()
            return False
        self._thread = threading.Thread(
            target=self._run,
            name=f"mem0-lease-{self.lease.id}",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                renewed = self.repository.renew_outbox(
                    self.lease,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                log.exception(
                    "Mem0 outbox lease renewal failed for %s: %s",
                    self.lease.id,
                    exc,
                )
                renewed = False
            if not renewed:
                self._lost.set()
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


class ProjectionWorker:
    """Lease, apply, ack, retry, and dead-letter projection outbox events."""

    def __init__(
        self,
        repository: CanonicalRepository,
        handlers: dict[str, Callable[[Lease], None]],
        *,
        consumer: str = "mem0-projection",
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_interval: float | None = None,
    ):
        self.repository = repository
        self.handlers = handlers
        self.consumer = consumer
        self.owner = f"{consumer}:{uuid.uuid4().hex}"
        self.retry_seconds = retry_seconds
        self.lease_seconds = max(0.001, float(lease_seconds))
        self.heartbeat_interval = max(
            0.001,
            float(
                heartbeat_interval
                if heartbeat_interval is not None
                else min(1.0, self.lease_seconds / 3)
            ),
        )

    def _process_lease(self, lease: Lease) -> str:
        heartbeat = _OutboxLeaseHeartbeat(
            self.repository,
            lease,
            lease_seconds=self.lease_seconds,
            interval_seconds=self.heartbeat_interval,
        )
        if not heartbeat.start():
            return "lost"
        try:
            handler = self.handlers.get(lease.projection)
            if handler is None:
                raise KeyError(f"no projection handler: {lease.projection}")
            handler(lease)
            if heartbeat.lost:
                raise RuntimeError(f"lost outbox lease {lease.id}")
            if not self.repository.ack_outbox(lease, self.consumer):
                raise RuntimeError(f"lost outbox lease {lease.id}")
            return "acked"
        except Exception as exc:
            return self.repository.retry_outbox(
                lease,
                exc,
                retry_seconds=self.retry_seconds,
            )
        finally:
            heartbeat.stop()

    def run_once(self, *, limit: int = 20, aggregate_id: str | None = None) -> dict:
        leases = self.repository.lease_outbox(
            self.owner,
            limit=limit,
            lease_seconds=self.lease_seconds,
            aggregate_id=aggregate_id,
        )
        result = {"leased": len(leases), "acked": 0, "retried": 0, "dead": 0}
        for lease in leases:
            status = self._process_lease(lease)
            if status == "acked":
                result["acked"] += 1
            elif status == "dead":
                result["dead"] += 1
            else:
                result["retried"] += 1
        return result

    def drain(self, *, aggregate_id: str | None = None, max_rounds: int = 20) -> dict:
        total = {"leased": 0, "acked": 0, "retried": 0, "dead": 0}
        for _ in range(max_rounds):
            current = self.run_once(aggregate_id=aggregate_id)
            for key in total:
                total[key] += current[key]
            if not current["leased"] or current["retried"]:
                break
        return total


class ProjectionConsumer:
    """Periodic owner for all persisted projection work and DLQ alerts."""

    def __init__(
        self,
        repository: CanonicalRepository,
        mem_factory: Callable,
        *,
        interval_seconds: float | None = None,
        notifier: Callable[[dict], None] | None = None,
    ):
        self.repository = repository
        self.mem_factory = mem_factory
        self.interval_seconds = float(
            interval_seconds
            if interval_seconds is not None
            else os.environ.get("MEM0_OUTBOX_POLL_SECONDS", "1")
        )
        self.notifier = notifier or _default_dlq_notifier
        self._stop = threading.Event()
        self._drain_deadline = float("inf")
        self._thread = threading.Thread(
            target=self._run,
            name=f"mem0-outbox-{hash(str(repository.path)) & 0xFFFF:x}",
            daemon=True,
        )
        self._alerted: set[int] = set()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def _alert_open_dead_letters(self) -> None:
        for item in self.repository.dead_letters(status="open"):
            if item["id"] in self._alerted:
                continue
            self.notifier(item)
            self._alerted.add(item["id"])

    def _run(self) -> None:
        while True:
            leased = 0
            try:
                mem = self.mem_factory()
                if mem is not None:
                    handlers = LegacyProjectionHandlers(self.repository, mem)
                    result = ProjectionWorker(
                        self.repository,
                        handlers.as_dict(),
                        retry_seconds=DEFAULT_RETRY_SECONDS,
                    ).run_once(limit=50)
                    leased = result["leased"]
                self._alert_open_dead_letters()
            except Exception as exc:
                log.exception("Mem0 projection consumer iteration failed: %s", exc)
            if self._stop.is_set():
                if (
                    time.monotonic() >= self._drain_deadline
                    or self.repository.unfinished_outbox_count() == 0
                ):
                    return
                remaining = self._drain_deadline - time.monotonic()
                if not leased and remaining > 0:
                    time.sleep(min(0.05, remaining))
                continue
            if not leased:
                self._stop.wait(max(0.05, self.interval_seconds))

    def stop(self, timeout: float = 5.0) -> bool:
        timeout = max(0.0, timeout)
        self._drain_deadline = time.monotonic() + timeout
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        return not self._thread.is_alive()


def _default_dlq_notifier(item: dict) -> None:
    log.critical(
        "MEM0_DLQ source=%s id=%s generation=%s error=%s",
        item.get("source_kind"),
        item.get("source_key"),
        item.get("generation"),
        item.get("last_error"),
    )


_consumers: dict[str, ProjectionConsumer] = {}
_consumers_lock = threading.Lock()


def ensure_projection_consumer(
    mem_factory: Callable,
    *,
    repository: CanonicalRepository | None = None,
    interval_seconds: float | None = None,
    notifier: Callable[[dict], None] | None = None,
) -> ProjectionConsumer:
    repository = repository or get_repository()
    key = str(repository.path)
    with _consumers_lock:
        consumer = _consumers.get(key)
        if consumer is None or not consumer.alive:
            consumer = ProjectionConsumer(
                repository,
                mem_factory,
                interval_seconds=interval_seconds,
                notifier=notifier,
            )
            _consumers[key] = consumer
            consumer.start()
    return consumer


def shutdown_projection_consumers(timeout: float = 5.0) -> None:
    with _consumers_lock:
        consumers = list(_consumers.items())
    for key, consumer in consumers:
        stopped = consumer.stop(timeout)
        if stopped:
            with _consumers_lock:
                if _consumers.get(key) is consumer:
                    del _consumers[key]


class LegacyProjectionHandlers:
    """Projection adapter for the existing Mem0/BM25/graph/fact stores."""

    def __init__(self, repository: CanonicalRepository, mem):
        self.repository = repository
        self.mem = mem
        self.results: dict[str, dict] = {}

    def as_dict(self) -> dict[str, Callable[[Lease], None]]:
        return {
            "vector": self.vector,
            "bm25": self.bm25,
            "graph": self.graph,
            "fact_store": self.fact_store,
        }

    def _fact(self, lease: Lease) -> dict:
        fact = self.repository.get_fact(lease.aggregate_id)
        if fact is None:
            raise KeyError(lease.aggregate_id)
        return fact

    def _lookup_vector_legacy_id(self, payload: dict, fact_id: str) -> str | None:
        """Return only a provider row proven to carry this canonical id.

        Mem0 providers do not all enforce ``filters`` consistently. Treat the
        response as an untrusted scan result: an empty, well-formed result
        permits an add, while unavailable or malformed results must retry
        rather than opening a duplicate-add window.
        """
        try:
            raw = self.mem.get_all(
                user_id=payload["user_id"],
                filters={"canonical_id": fact_id},
            )
        except Exception as exc:
            raise RuntimeError(
                "vector idempotency lookup failed; refusing duplicate add",
            ) from exc

        if raw is None:
            raise RuntimeError(
                "vector idempotency lookup returned None; refusing duplicate add",
            )
        if isinstance(raw, dict):
            if "error" in raw:
                raise RuntimeError(
                    "vector idempotency lookup returned an error; "
                    "refusing duplicate add",
                )
            if "results" not in raw:
                raise RuntimeError(
                    "vector idempotency lookup returned an unknown shape; "
                    "refusing duplicate add",
                )
            rows = raw["results"]
        elif isinstance(raw, list):
            rows = raw
        else:
            raise RuntimeError(
                "vector idempotency lookup returned an unknown shape; "
                "refusing duplicate add",
            )
        if not isinstance(rows, list):
            raise RuntimeError(
                "vector idempotency lookup results are not a list; "
                "refusing duplicate add",
            )

        matches: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(
                    "vector idempotency lookup returned a malformed row; "
                    "refusing duplicate add",
                )
            metadata = row.get("metadata")
            if not isinstance(metadata, dict):
                raise RuntimeError(
                    "vector idempotency lookup row metadata is unverifiable; "
                    "refusing duplicate add",
                )
            if metadata.get("canonical_id") != fact_id:
                continue
            legacy_id = row.get("id")
            if not isinstance(legacy_id, (str, int)) or not str(legacy_id):
                raise RuntimeError(
                    "vector idempotency lookup matched a row without an id; "
                    "refusing duplicate add",
                )
            matches.append(str(legacy_id))
        return matches[0] if matches else None

    def vector(self, lease: Lease) -> None:
        payload = lease.payload
        fact_id = str(payload.get("fact_id") or lease.aggregate_id)
        current = self.repository.get_fact(fact_id) or {}
        legacy_id = payload.get("legacy_id") or current.get("legacy_id")
        if lease.operation == "upsert":
            if legacy_id:
                return
            metadata = dict(payload.get("metadata") or {})
            metadata["canonical_id"] = fact_id
            # Close the provider-commit/local-bind crash window. A retry first
            # searches for the stable canonical idempotency key before adding.
            legacy_id = self._lookup_vector_legacy_id(payload, fact_id)
            if legacy_id:
                self.repository.bind_legacy_id(fact_id, legacy_id)
                return
            if not self.repository.renew_outbox(lease):
                raise RuntimeError(f"lost outbox lease {lease.id} before vector add")
            result = self.mem.add(
                [{"role": "user", "content": payload["text"]}],
                user_id=payload["user_id"],
                metadata=metadata,
                infer=False,
            )
            rows = result.get("results", []) if isinstance(result, dict) else []
            legacy_id = next(
                (
                    row.get("id")
                    for row in rows
                    if isinstance(row, dict) and row.get("id")
                ),
                None,
            )
            if not legacy_id:
                raise RuntimeError("vector projection returned no memory id")
            self.repository.bind_legacy_id(fact_id, str(legacy_id))
            self.results[fact_id] = result
            return
        if not legacy_id:
            legacy_id = self._lookup_vector_legacy_id(payload, fact_id)
            if not legacy_id and lease.operation in ("archive", "delete"):
                return
            if not legacy_id:
                raise RuntimeError("vector projection has no legacy id")
        if lease.operation == "delete":
            if not self.repository.renew_outbox(lease):
                raise RuntimeError(
                    f"lost outbox lease {lease.id} before vector delete",
                )
            self.mem.delete(legacy_id)
            return
        patch = payload.get("metadata_patch") or payload.get("metadata") or {}
        if not self.repository.renew_outbox(lease):
            raise RuntimeError(f"lost outbox lease {lease.id} before vector update")
        self.mem.update(
            memory_id=legacy_id,
            data=payload["text"],
            metadata=patch,
        )

    def bm25(self, lease: Lease) -> None:
        from services.mem0 import bm25_index
        from services.mem0._projection import _internal_projection_capability

        payload = lease.payload
        projection_id = payload["fact_id"]
        if not bm25_index.apply_projection(
            lease.key,
            lease.operation,
            projection_id,
            payload["text"],
            payload["user_id"],
            _capability=_internal_projection_capability(),
        ):
            raise RuntimeError("BM25 projection failed")

    def graph(self, lease: Lease) -> None:
        from services.mem0 import graph
        from services.mem0._projection import _internal_projection_capability

        capability = _internal_projection_capability()
        if lease.operation == "relation_upsert":
            if not graph.apply_relation_projection(
                lease.key,
                lease.payload.get("src_name", ""),
                lease.payload.get("rel_type", ""),
                lease.payload.get("dst_name", ""),
                lease.payload.get("user_id", ""),
                _capability=capability,
            ):
                raise RuntimeError("graph relation projection failed")
            return
        payload = lease.payload
        projection_id = payload["fact_id"]
        if not graph.apply_fact_projection(
            lease.key,
            projection_id,
            payload["user_id"],
            payload.get("entities") or [],
            delete=lease.operation in ("archive", "delete"),
            _capability=capability,
        ):
            raise RuntimeError("graph projection failed")

    def fact_store(self, lease: Lease) -> None:
        from services.mem0 import fact_store
        from services.mem0._projection import _internal_projection_capability

        payload = lease.payload
        projection_id = payload["fact_id"]
        metadata = payload.get("metadata") or {}
        attribute = str(metadata.get("attribute") or "")
        ok = fact_store.apply_projection(
            lease.key,
            lease.operation,
            projection_id,
            payload["user_id"],
            attribute,
            value_text=payload["text"],
            value_json=metadata.get("value_json"),
            condition=str(metadata.get("condition") or ""),
            scope=str(metadata.get("scope") or ""),
            source_conversation_id=(
                str(metadata.get("channel_id")) if metadata.get("channel_id") else None
            ),
            merged_into=payload.get("merged_into"),
            _capability=_internal_projection_capability(),
        )
        if not ok:
            raise RuntimeError("fact-store projection failed")


def _provider_storage_identity(mem) -> ProviderStorageIdentity:
    value = getattr(mem, "storage_identity", None)
    if callable(value):
        value = value()
    if isinstance(value, ProviderStorageIdentity):
        identity = value
    elif isinstance(value, dict):
        identity = ProviderStorageIdentity(
            backend=str(value.get("backend") or "").strip(),
            endpoint=str(value.get("endpoint") or "").strip(),
            collection=str(
                value.get("collection") or value.get("index") or "",
            ).strip(),
        )
    else:
        raise RuntimeError("provider storage identity is unavailable")
    if not all(identity.cache_key()):
        raise RuntimeError("provider storage identity is incomplete")
    return identity


def _provider_rows_for_namespace(raw, user_id: str) -> tuple[list[dict], list[str]]:
    rows = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise RuntimeError("canonical migration coverage could not be verified")
    verified: list[dict] = []
    provider_ids: list[str] = []
    requested = str(user_id)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RuntimeError(f"provider row {index} is not an object")
        provider_id = row.get("id") or row.get("memory_id")
        if not provider_id:
            raise RuntimeError(f"provider row {index} has no stable id")
        metadata = row.get("metadata")
        claims = []
        if row.get("user_id") is not None:
            claims.append(str(row["user_id"]))
        if isinstance(metadata, dict) and metadata.get("user_id") is not None:
            claims.append(str(metadata["user_id"]))
        if not claims or any(claim != requested for claim in claims):
            raise RuntimeError(
                f"provider row {provider_id} does not prove requested namespace",
            )
        verified.append(row)
        provider_ids.append(str(provider_id))
    return verified, provider_ids


class MemoryApplicationService:
    """Single mutation boundary for memory lifecycle operations."""

    def __init__(
        self,
        repository: CanonicalRepository | None = None,
        *,
        mode: str | None = None,
    ):
        self.mode = (mode or os.environ.get("MEM0_INTEGRITY_MODE", "legacy")).lower()
        if self.mode not in {"legacy", "shadow", "canonical"}:
            raise ValueError(f"invalid MEM0_INTEGRITY_MODE: {self.mode}")
        self.repository = repository
        if self.repository is None and self.mode != "legacy":
            self.repository = get_repository()

    @property
    def canonical_enabled(self) -> bool:
        return self.mode in {"shadow", "canonical"}

    def _worker(
        self, mem, *, retry_seconds: float = 0.0
    ) -> tuple[ProjectionWorker, LegacyProjectionHandlers]:
        if self.repository is None:
            raise RuntimeError("canonical repository is unavailable in legacy mode")
        handlers = LegacyProjectionHandlers(self.repository, mem)
        worker = ProjectionWorker(
            self.repository,
            handlers.as_dict(),
            retry_seconds=retry_seconds,
        )
        return worker, handlers

    def _ensure_canonical_ready(self, mem, user_id: str) -> None:
        if self.mode != "canonical":
            return
        provider_identity = _provider_storage_identity(mem)
        key = (
            str(self.repository.path),
            *provider_identity.cache_key(),
            str(user_id),
        )
        if key in _verified_migrations:
            return
        raw = mem.get_all(user_id=str(user_id))
        _, provider_ids = _provider_rows_for_namespace(raw, str(user_id))
        self.repository.record_migration_coverage(
            str(user_id),
            provider_ids,
            provider_identity=provider_identity,
        )
        _verified_migrations.add(key)

    def verify_canonical_startup(self, mem) -> None:
        """Validate every known migration namespace before canonical startup."""
        if self.mode != "canonical":
            return
        for user_id in self.repository.migration_users():
            self._ensure_canonical_ready(mem, user_id)

    def create_fact(
        self,
        mem,
        *,
        user_id: str,
        text: str,
        metadata: dict,
        entities: list[dict] | None = None,
    ) -> dict:
        if not self.canonical_enabled:
            return mem.add(
                [{"role": "user", "content": text}],
                user_id=user_id,
                metadata=metadata,
                infer=False,
            )
        self._ensure_canonical_ready(mem, user_id)
        source = str(metadata.get("source") or "")
        idempotency_key = str(metadata.get("idempotency_key") or "")
        if source not in {"batch", "replay"}:
            idempotency_key = ""
        fact_id = None
        existing = None
        if idempotency_key:
            fact_id = hashlib.sha256(
                f"{source}\0{idempotency_key}\0{user_id}\0{text}".encode(),
            ).hexdigest()
            existing = self.repository.get_fact(fact_id)
            if existing is not None and (
                existing["user_id"] != str(user_id) or existing["text"] != text
            ):
                raise RuntimeError(
                    f"{source} idempotency key conflicts with canonical fact",
                )
        fact = (
            self.repository.create_fact(
                user_id,
                text,
                metadata,
                entities=entities,
                fact_id=fact_id,
            )
            if existing is None
            else existing
        )
        worker, handlers = self._worker(mem)
        worker.drain(aggregate_id=fact["fact_id"])
        result = handlers.results.get(fact["fact_id"])
        if result is not None:
            result = dict(result)
            result["canonical_id"] = fact["fact_id"]
            result["_projections_managed"] = True
            return result
        current = self.repository.get_fact(fact["fact_id"])
        return {
            "results": [
                {
                    "id": current.get("legacy_id") or current["fact_id"],
                    "memory": current["text"],
                }
            ],
            "canonical_id": fact["fact_id"],
            "_projections_managed": True,
            "projection_pending": True,
        }

    def ingest_legacy_messages(
        self,
        mem,
        messages: list[dict],
        *,
        user_id: str,
        metadata: dict,
    ) -> dict:
        """Run the compatibility extractor behind the mutation boundary.

        Legacy mode preserves the historical Mem0 call exactly. During a
        staged migration Mem0 may still perform inference, but every returned
        provider row is immediately adopted into the canonical ledger and its
        non-vector projection intents are committed with that canonical write.
        The vector row itself is treated as an already-materialized projection.
        """
        if self.mode == "canonical":
            raise RuntimeError(
                "legacy inference is provider-first and is disabled in canonical mode",
            )
        result = mem.add(messages, user_id=user_id, metadata=metadata)
        if not self.canonical_enabled or not isinstance(result, dict):
            return result

        rows = result.get("results") or []
        if not isinstance(rows, list):
            return result
        worker, _ = self._worker(mem)
        canonical_ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            record = dict(row)
            legacy_id = record.get("id") or record.get("memory_id")
            if legacy_id and not (record.get("memory") or record.get("text")):
                fetched = mem.get(str(legacy_id))
                if isinstance(fetched, dict):
                    record = fetched
            row_metadata = dict(metadata)
            row_metadata.update(record.get("metadata") or {})
            record["metadata"] = row_metadata
            record.setdefault("user_id", str(user_id))
            try:
                fact = self.repository.adopt_legacy_fact(record, user_id=user_id)
            except (ValueError, sqlite3.IntegrityError):
                log.warning(
                    "Mem0 compatibility result could not be adopted: id=%s",
                    legacy_id,
                )
                continue
            canonical_ids.append(fact["fact_id"])
            worker.drain(aggregate_id=fact["fact_id"])

        managed = dict(result)
        managed["canonical_ids"] = canonical_ids
        managed["_projections_managed"] = True
        managed["projection_pending"] = any(
            any(
                row["status"] not in TERMINAL_STATUSES
                for row in self.repository.outbox_rows(aggregate_id=fact_id)
            )
            for fact_id in canonical_ids
        )
        return managed

    def _ensure_legacy(
        self, mem, identifier: str, *, user_id: str | None = None
    ) -> dict:
        existing = self.repository.get_fact(identifier)
        if existing:
            self._ensure_canonical_ready(mem, existing["user_id"])
            return existing
        record = mem.get(identifier)
        if not isinstance(record, dict):
            if self.mode == "canonical":
                raise KeyError(identifier)
            record = {
                "id": identifier,
                "memory": f"legacy memory {identifier}",
                "metadata": {},
                "user_id": user_id or "legacy",
            }
        uid = str(
            user_id
            or record.get("user_id")
            or (record.get("metadata") or {}).get("user_id")
            or "legacy"
        )
        self._ensure_canonical_ready(mem, uid)
        return self.repository.adopt_legacy_fact(record, user_id=user_id)

    def delete(self, mem, identifier: str) -> bool:
        if not self.canonical_enabled:
            mem.delete(identifier)
            return True
        fact = self._ensure_legacy(mem, identifier)
        self.repository.delete_fact(fact["fact_id"])
        worker, _ = self._worker(mem)
        outcome = worker.drain(aggregate_id=fact["fact_id"])
        return outcome["dead"] == 0 and outcome["retried"] == 0

    def archive(
        self,
        mem,
        identifier: str,
        *,
        reason: str,
        merged_into: str | None = None,
    ) -> bool:
        if not self.canonical_enabled:
            record = mem.get(identifier)
            if not isinstance(record, dict):
                return False
            text = record.get("memory") or record.get("text") or ""
            if not text:
                return False
            patch = {"valid_to": int(time.time()), "conflict_archived_as": reason}
            if merged_into:
                patch["consolidated_into"] = merged_into
            mem.update(memory_id=identifier, data=text, metadata=patch)
            return True
        fact = self._ensure_legacy(mem, identifier)
        self.repository.archive_fact(
            fact["fact_id"],
            reason=reason,
            merged_into=merged_into,
        )
        worker, _ = self._worker(mem)
        outcome = worker.drain(aggregate_id=fact["fact_id"])
        return outcome["dead"] == 0 and outcome["retried"] == 0

    def reinforce(
        self,
        mem,
        identifier: str,
        *,
        channel: str,
        min_interval: int = 0,
    ) -> bool:
        fact = self.repository.get_fact(identifier) if self.canonical_enabled else None
        if fact is not None:
            self._ensure_canonical_ready(mem, fact["user_id"])
            text = fact["text"]
            metadata = dict(fact.get("metadata") or {})
        else:
            record = mem.get(identifier)
            if not isinstance(record, dict):
                return False
            text = record.get("memory") or record.get("text") or ""
            if not text:
                return False
            metadata = dict(record.get("metadata") or {})
        now = int(time.time())
        if channel == "recall":
            try:
                last = int(metadata.get("last_recalled_at") or 0)
            except (TypeError, ValueError):
                last = 0
            if last and now - last < min_interval:
                return False
            history = list(metadata.get("reinforced_at") or [])
            history.append(now)
            try:
                count = int(metadata.get("recall_count") or 0)
            except (TypeError, ValueError):
                count = 0
            patch = {
                "recall_count": count + 1,
                "reinforced_at": history[-10:],
                "last_reinforced_at": now,
                "last_recalled_at": now,
            }
        else:
            history = list(metadata.get("reinforced_at") or [])
            history.append(now)
            try:
                count = int(metadata.get("mention_count", 1))
            except (TypeError, ValueError):
                count = 1
            patch = {
                "mention_count": count + 1,
                "reinforced_at": history[-10:],
                "last_reinforced_at": now,
            }
        if not self.canonical_enabled:
            mem.update(memory_id=identifier, data=text, metadata=patch)
            return True
        if fact is None:
            fact = self._ensure_legacy(mem, identifier)
        self.repository.patch_metadata(
            fact["fact_id"],
            patch,
            operation="reinforce",
            projections=("vector",),
        )
        worker, _ = self._worker(mem)
        outcome = worker.drain(aggregate_id=fact["fact_id"])
        return outcome["dead"] == 0 and outcome["retried"] == 0

    def update_metadata(
        self,
        mem,
        identifier: str,
        patch: dict,
        *,
        record: dict | None = None,
        operation: str = "update",
    ) -> bool:
        """Update canonical metadata and project it to the legacy provider."""
        fact = self.repository.get_fact(identifier) if self.canonical_enabled else None
        if fact is not None:
            self._ensure_canonical_ready(mem, fact["user_id"])
            source = fact
            text = fact["text"]
        else:
            source = record if isinstance(record, dict) else mem.get(identifier)
            if not isinstance(source, dict):
                return False
            text = source.get("memory") or source.get("text") or ""
            if not text:
                return False
        if not self.canonical_enabled:
            mem.update(memory_id=identifier, data=text, metadata=dict(patch))
            return True
        if fact is None:
            fact = self._ensure_legacy(mem, identifier)
        self.repository.patch_metadata(
            fact["fact_id"],
            patch,
            operation=operation,
            projections=("vector",),
        )
        worker, _ = self._worker(mem)
        outcome = worker.drain(aggregate_id=fact["fact_id"])
        return outcome["dead"] == 0 and outcome["retried"] == 0

    def consolidate(
        self,
        mem,
        *,
        user_id: str,
        source_ids: list[str],
        outputs: list[dict],
        reason: str = "consolidated",
        source_records: dict[str, dict] | None = None,
    ) -> list[dict]:
        if not self.canonical_enabled:
            created: list[dict] = []
            created_ids: list[str] = []
            for output in outputs:
                result = self.create_fact(
                    mem,
                    user_id=user_id,
                    text=str(output.get("text") or ""),
                    metadata=dict(output.get("metadata") or {}),
                    entities=list(output.get("entities") or []),
                )
                rows = result.get("results", []) if isinstance(result, dict) else []
                legacy_id = next(
                    (
                        row.get("id")
                        for row in rows
                        if isinstance(row, dict) and row.get("id")
                    ),
                    None,
                )
                if legacy_id:
                    created_ids.append(str(legacy_id))
                    created.append(
                        {
                            "fact_id": str(legacy_id),
                            "legacy_id": str(legacy_id),
                            "user_id": str(user_id),
                            "text": output.get("text", ""),
                            "metadata": dict(output.get("metadata") or {}),
                            "entities": list(output.get("entities") or []),
                            "status": "active",
                        }
                    )
            merged_into = ",".join(created_ids) or None
            for source_id in source_ids:
                record = (source_records or {}).get(source_id)
                if isinstance(record, dict):
                    text = record.get("memory") or record.get("text") or ""
                    patch = {
                        "valid_to": int(time.time()),
                        "conflict_archived_as": reason,
                        "consolidated_into": merged_into or "",
                    }
                    mem.update(memory_id=source_id, data=text, metadata=patch)
                else:
                    self.archive(
                        mem,
                        source_id,
                        reason=reason,
                        merged_into=merged_into,
                    )
            return created
        self._ensure_canonical_ready(mem, user_id)
        for source_id in source_ids:
            record = (source_records or {}).get(source_id)
            if isinstance(record, dict):
                self.repository.adopt_legacy_fact(record, user_id=user_id)
            else:
                self._ensure_legacy(mem, source_id, user_id=user_id)
        created = self.repository.consolidate(
            user_id,
            source_ids,
            outputs,
            reason=reason,
        )
        worker, _ = self._worker(mem)
        for fact in created:
            worker.drain(aggregate_id=fact["fact_id"])
        for source_id in source_ids:
            source = self.repository.get_fact(source_id)
            if source:
                worker.drain(aggregate_id=source["fact_id"])
        return created

    def merge(
        self,
        mem,
        *,
        user_id: str,
        source_ids: list[str],
        text: str,
        metadata: dict | None = None,
        entities: list[dict] | None = None,
    ) -> dict:
        created = self.consolidate(
            mem,
            user_id=user_id,
            source_ids=source_ids,
            outputs=[
                {
                    "text": text,
                    "metadata": metadata or {"source": "merge"},
                    "entities": entities or [],
                }
            ],
            reason="merge",
        )
        return created[0]

    def record_relations(self, mem, user_id: str, relations: list[dict]) -> int:
        """Persist extracted relations through the canonical graph boundary."""
        if not relations:
            return 0
        if not self.canonical_enabled:
            from services.mem0 import graph

            added = 0
            for relation in relations:
                src_name = relation.get("src")
                rel_type = relation.get("rel_type")
                dst_name = relation.get("dst")
                if not (src_name and rel_type and dst_name):
                    continue
                src = graph.find_entity(src_name, str(user_id), fuzzy=False)
                dst = graph.find_entity(dst_name, str(user_id), fuzzy=False)
                if not (src and dst):
                    continue
                if graph.add_relation(src.id, rel_type, dst.id, str(user_id)):
                    added += 1
            return added

        self._ensure_canonical_ready(mem, user_id)
        canonical = self.repository.create_relations(str(user_id), relations)
        worker, _ = self._worker(mem)
        worker.drain()
        return len(canonical)

    def backfill_legacy(self, mem, user_id: str, *, limit: int | None = None) -> dict:
        """Adopt existing provider rows without rewriting the vector projection."""
        if self.mode != "shadow":
            raise RuntimeError("legacy backfill must run in shadow mode before cutover")
        kwargs = {"user_id": user_id}
        if limit is not None:
            kwargs["limit"] = limit
        raw = mem.get_all(**kwargs)
        provider_identity = _provider_storage_identity(mem)
        rows, provider_ids = _provider_rows_for_namespace(raw, str(user_id))
        adopted = 0
        projected = 0
        worker, _ = self._worker(mem)
        for row in rows:
            try:
                fact = self.repository.adopt_legacy_fact(row, user_id=user_id)
            except (ValueError, sqlite3.IntegrityError):
                continue
            adopted += 1
            projected += worker.drain(aggregate_id=fact["fact_id"])["acked"]
        coverage = None
        if limit is None:
            coverage = self.repository.record_migration_coverage(
                str(user_id),
                provider_ids,
                provider_identity=provider_identity,
            )
        return {
            "scanned": len(rows),
            "adopted": adopted,
            "projected": projected,
            "coverage": coverage,
        }

    async def replay(self, payload: dict, ingest: Callable) -> dict | None:
        """Execute one leased replay item through the normal ingest boundary."""
        if payload.get("skip"):
            return {"kept": 0}
        return await ingest(
            user_text=payload.get("user_text", ""),
            assistant_text=payload.get("assistant_text", ""),
            user_id=payload.get("user_id"),
            metadata={
                "source": "replay",
                "channel_id": payload.get("channel_id"),
                "idempotency_key": payload.get("idempotency_key"),
            },
        )


_repositories: dict[str, CanonicalRepository] = {}
_repositories_lock = threading.Lock()
_verified_migrations: set[tuple[str, str, str, str, str]] = set()


def get_repository(path: str | os.PathLike | None = None) -> CanonicalRepository:
    resolved = str(Path(path) if path is not None else _default_db_path())
    with _repositories_lock:
        repository = _repositories.get(resolved)
        if repository is None:
            repository = CanonicalRepository(resolved)
            _repositories[resolved] = repository
    return repository


def get_application_service(
    *,
    path: str | os.PathLike | None = None,
    mode: str | None = None,
) -> MemoryApplicationService:
    selected = (mode or os.environ.get("MEM0_INTEGRITY_MODE", "legacy")).lower()
    repository = (
        get_repository(path) if selected != "legacy" or path is not None else None
    )
    return MemoryApplicationService(repository, mode=selected)


def reset_repository_cache_for_tests() -> None:
    shutdown_projection_consumers(timeout=1.0)
    with _repositories_lock:
        _repositories.clear()
        _verified_migrations.clear()


def canonical_fact_visible(
    identifier: str,
    user_id: str,
    *,
    current_channel_id: str | None = None,
    mode: str | None = None,
) -> bool:
    """Apply canonical lifecycle and scope policy to a projection result.

    Missing rows remain visible in ``legacy``/``shadow`` so backfill can be
    gradual. ``canonical`` mode fails closed once migration coverage is
    declared complete.
    """
    selected = (mode or os.environ.get("MEM0_INTEGRITY_MODE", "legacy")).lower()
    if selected == "legacy":
        return True
    if not user_id:
        return False
    fact = get_repository().get_fact(identifier)
    if fact is None:
        return selected != "canonical"
    if fact["user_id"] != str(user_id) or fact["status"] != "active":
        return False
    metadata = fact.get("metadata") or {}
    valid_to = metadata.get("valid_to")
    if valid_to:
        try:
            if int(valid_to) <= int(time.time()):
                return False
        except (TypeError, ValueError):
            return False
    scope = str(metadata.get("scope") or "global")
    if scope.startswith("conversation"):
        expected = f"conversation:{current_channel_id}" if current_channel_id else ""
        if scope != expected:
            return False
    return True


atexit.register(shutdown_projection_consumers)

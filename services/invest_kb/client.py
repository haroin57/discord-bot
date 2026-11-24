"""LanceDB connection + schema for invest_kb.

LanceDB is embedded — no daemon, no port. The `.state/invest_kb/` directory
holds the Lance dataset. Schema is fixed (Arrow) so we can rely on column
order/typing in search.py without runtime introspection.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import threading
import weakref
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import pyarrow as pa

log = logging.getLogger(__name__)

EMBED_DIM = 1536  # OpenAI text-embedding-3-small
TABLE_NAME = "invest_kb"
CHUNK_ID_PREFIX = "invest-kb:v2:"

_db_cache: dict = {"db": None, "path": None}
_db_cache_lock = threading.RLock()


@dataclass
class _DocumentLockEntry:
    lock: asyncio.Lock
    users: int = 0


_document_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def make_chunk_id(source: str, source_id: str, chunk_index: int) -> str:
    """Return the stable, collision-resistant ID for one document chunk.

    Legacy IDs joined the three fields with ``:`` and therefore could not
    distinguish values containing that delimiter. Canonical JSON preserves the
    field boundaries; the versioned prefix keeps new IDs disjoint from every
    legacy ID and leaves room for a future format migration.
    """
    source = str(source or "")
    source_id = str(source_id or "")
    chunk_index = int(chunk_index)
    if not source or not source_id:
        raise ValueError("chunk IDs require non-empty source and source_id")
    if chunk_index < 0:
        raise ValueError("chunk index must be non-negative")
    canonical = json.dumps(
        {
            "chunk_index": chunk_index,
            "source": source,
            "source_id": source_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(b"invest-kb/chunk-id/v2\0" + canonical).hexdigest()
    return f"{CHUNK_ID_PREFIX}{digest}"


def _resolve_path() -> str:
    from config import INVEST_KB_PATH

    return INVEST_KB_PATH


def get_db(path: Optional[str] = None):
    """Return an open LanceDB connection. Cached per-path."""
    import lancedb

    p = path or _resolve_path()
    with _db_cache_lock:
        os.makedirs(p, exist_ok=True)
        if _db_cache["db"] is not None and _db_cache["path"] == p:
            return _db_cache["db"]
        db = lancedb.connect(p)
        _db_cache["db"] = db
        _db_cache["path"] = p
        return db


def schema() -> pa.Schema:
    """Arrow schema for the invest_kb table.

    `vector_body` is recall over raw chunk text; `vector_summary` over the
    Haiku-generated 90-char summary. Searching both and taking max-sim catches
    "long chunk where only a sub-section matches" (body would be diluted) and
    "tight keyword match" (summary may have abstracted away the keyword) at the
    same time.
    """
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("summary", pa.string()),
            pa.field("keywords", pa.string()),  # CSV
            pa.field("entities", pa.string()),  # CSV
            pa.field("vector_body", pa.list_(pa.float32(), EMBED_DIM)),
            pa.field("vector_summary", pa.list_(pa.float32(), EMBED_DIM)),
            pa.field("source", pa.string()),
            pa.field("source_id", pa.string()),
            pa.field("source_url", pa.string()),
            pa.field("title", pa.string()),
            pa.field("lang", pa.string()),
            pa.field("license", pa.string()),
            pa.field("published_at", pa.int64()),  # unix ts, 0 if unknown
            pa.field("category", pa.string()),
        ]
    )


def get_table(path: Optional[str] = None):
    """Open or create the invest_kb table."""
    with _db_cache_lock:
        db = get_db(path)
        names = db.list_tables().tables
        if TABLE_NAME in names:
            return db.open_table(TABLE_NAME)
        log.info("Creating LanceDB table %r at %s", TABLE_NAME, _db_cache["path"])
        import pyarrow as pa

        empty = pa.Table.from_pylist([], schema=schema())
        return db.create_table(TABLE_NAME, data=empty)


async def get_table_async(path: Optional[str] = None):
    """Open or create the table without blocking the event loop."""
    return await asyncio.to_thread(get_table, path)


def _sql_literal(value: str) -> str:
    """Return a single-quoted SQL literal for a LanceDB filter."""
    return "'" + value.replace("'", "''") + "'"


def _validate_document_rows(rows: list[dict]) -> tuple[str, str]:
    if not rows:
        raise ValueError("replace_document requires at least one chunk")

    source = str(rows[0].get("source") or "")
    source_id = str(rows[0].get("source_id") or "")
    if not source or not source_id:
        raise ValueError("replace_document requires non-empty source and source_id")

    ids: set[str] = set()
    for row in rows:
        if str(row.get("source") or "") != source:
            raise ValueError("replace_document rows must have one source")
        if str(row.get("source_id") or "") != source_id:
            raise ValueError("replace_document rows must have one source_id")
        row_id = str(row.get("id") or "")
        if not row_id:
            raise ValueError("replace_document rows require non-empty ids")
        if row_id in ids:
            raise ValueError(f"replace_document received duplicate id: {row_id}")
        ids.add(row_id)
    return source, source_id


def _replace_document_sync(
    rows: list[dict],
    *,
    path: Optional[str] = None,
):
    """Atomically replace every chunk belonging to one source document.

    LanceDB's merge operation creates one table version containing all inserts,
    updates, and deletion of stale chunks. This also migrates legacy
    ``source:source_id:index`` rows: their IDs do not match the v2 source rows,
    so the document-scoped stale-row condition removes them in the same commit
    that inserts the v2 rows. If the commit fails, the previous table version
    remains visible. Retrying the same deterministic chunk rows is idempotent.
    """
    source, source_id = _validate_document_rows(rows)
    condition = (
        f"source = {_sql_literal(source)} AND source_id = {_sql_literal(source_id)}"
    )
    table = get_table(path)
    return (
        table.merge_insert("id")
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .when_not_matched_by_source_delete(condition)
        .execute(rows, on_bad_vectors="error")
    )


def _acquire_document_lock_entry(key: tuple[str, str, str]) -> _DocumentLockEntry:
    loop = asyncio.get_running_loop()
    entries = _document_locks.setdefault(loop, {})
    entry = entries.get(key)
    if entry is None:
        entry = _DocumentLockEntry(lock=asyncio.Lock())
        entries[key] = entry
    entry.users += 1
    return entry


def _release_document_lock_entry(
    key: tuple[str, str, str],
    entry: _DocumentLockEntry,
) -> None:
    loop = asyncio.get_running_loop()
    entries = _document_locks.get(loop)
    if entries is None:
        return
    entry.users -= 1
    if entry.users == 0 and entries.get(key) is entry:
        del entries[key]
    if not entries:
        del _document_locks[loop]


async def _run_replace_in_thread(rows: list[dict], path: Optional[str]):
    """Run a commit in a thread and keep its document lock through cancellation."""
    task = asyncio.create_task(
        asyncio.to_thread(_replace_document_sync, rows, path=path),
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            log.exception("replace_document commit failed after caller cancellation")
        raise


async def replace_document(
    rows: list[dict],
    *,
    path: Optional[str] = None,
    max_attempts: int = 3,
    retry_delay_s: float = 0.05,
) -> dict:
    """Atomically and idempotently replace one document's complete chunk set.

    Calls for the same ``(path, source, source_id)`` are serialized within an
    event loop. LanceDB handles cross-process commit conflicts; this wrapper
    retries failures because an exception can also arrive after a successful
    commit. The deterministic merge makes that uncertain-commit retry safe.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_delay_s < 0:
        raise ValueError("retry_delay_s must be non-negative")

    stable_rows = copy.deepcopy(rows)
    source, source_id = _validate_document_rows(stable_rows)
    resolved_path = path or _resolve_path()
    key = (resolved_path, source, source_id)
    entry = _acquire_document_lock_entry(key)
    try:
        async with entry.lock:
            for attempt in range(1, max_attempts + 1):
                try:
                    result = await _run_replace_in_thread(stable_rows, resolved_path)
                    return {
                        "source": source,
                        "source_id": source_id,
                        "chunks": len(stable_rows),
                        "attempts": attempt,
                        "result": result,
                    }
                except Exception as exc:
                    if attempt == max_attempts:
                        raise
                    delay = retry_delay_s * (2 ** (attempt - 1))
                    log.warning(
                        "replace_document failed for %s:%s (attempt %d/%d): %s",
                        source,
                        source_id,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    if delay:
                        await asyncio.sleep(delay)
    finally:
        _release_document_lock_entry(key, entry)


def table_stats(path: Optional[str] = None) -> dict:
    """Cheap stats: total rows, rows per source."""
    tbl = get_table(path)
    df = tbl.to_pandas(columns=["source"])
    by_source = df["source"].value_counts().to_dict() if len(df) else {}
    return {"total": len(df), "by_source": by_source}


async def table_stats_async(path: Optional[str] = None) -> dict:
    """Return table statistics without blocking the event loop."""
    return await asyncio.to_thread(table_stats, path)

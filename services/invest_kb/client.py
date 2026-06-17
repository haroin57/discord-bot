"""LanceDB connection + schema for invest_kb.

LanceDB is embedded — no daemon, no port. The `.state/invest_kb/` directory
holds the Lance dataset. Schema is fixed (Arrow) so we can rely on column
order/typing in search.py without runtime introspection.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import pyarrow as pa

log = logging.getLogger(__name__)

EMBED_DIM = 1536  # OpenAI text-embedding-3-small
TABLE_NAME = "invest_kb"

_db_cache: dict = {"db": None, "path": None}


def _resolve_path() -> str:
    from config import INVEST_KB_PATH
    return INVEST_KB_PATH


def get_db(path: Optional[str] = None):
    """Return an open LanceDB connection. Cached per-path."""
    import lancedb
    p = path or _resolve_path()
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
    db = get_db(path)
    names = db.table_names()
    if TABLE_NAME in names:
        return db.open_table(TABLE_NAME)
    log.info("Creating LanceDB table %r at %s", TABLE_NAME, _db_cache["path"])
    empty = pa.Table.from_pylist([], schema=schema())
    return db.create_table(TABLE_NAME, data=empty)


def table_stats(path: Optional[str] = None) -> dict:
    """Cheap stats: total rows, rows per source."""
    tbl = get_table(path)
    df = tbl.to_pandas(columns=["source"])
    by_source = df["source"].value_counts().to_dict() if len(df) else {}
    return {"total": len(df), "by_source": by_source}

"""BM25 keyword index over the per-channel JSONL conversation transcripts.

The Mem0 BM25 index (bm25_index.py) only contains *curated* facts — text
that survived extraction + the importance/dedup gate. A proper noun
mentioned once in passing ("東洋水産") never becomes a fact, so neither
vector nor fact-BM25 recall can surface it later.

This module indexes the RAW conversation lines instead. Every line the host
application logs can be mirrored here so a per-turn literal-keyword search
can rescue exact terms that curation dropped.

Store: a sqlite FTS5 virtual table at ``STATE_DIR/history_bm25.sqlite`` — a
SEPARATE file from ``mem0_bm25.sqlite`` (the entity-fact index) so the two
can be rebuilt or wiped independently. Tokenizer is ``unicode61`` (no CJK
segmentation, no substring match): callers pre-tokenize through the morpho
module and store the whitespace-joined content words in the indexed
``text`` column. The verbatim line stays in the UNINDEXED ``orig_text``
column and is what ``search_history`` returns as ``memory``; the indexed
``text`` column is for matching only and must never be surfaced to an LLM.

Scope: search is keyed by ``channel_id`` so a turn only ever sees its own
channel's history. The transcript has no author id, so host integrations
should gate history search to trusted callers.

The module is synchronous; callers wrap with ``asyncio.to_thread`` from
event loops. Connection plumbing (WAL-once init — the ADR-0007 fix for the
~30% concurrent-write row-drop bug — busy_timeout, locked writes with
retry) lives in :mod:`llm_mem0.sqlite_store`.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .sqlite_store import SqliteStore, build_fts_match_query

log = logging.getLogger(__name__)


_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
    channel_id UNINDEXED,
    ts UNINDEXED,
    role UNINDEXED,
    author UNINDEXED,
    orig_text UNINDEXED,
    text,
    tokenize = 'unicode61 remove_diacritics 1'
);
CREATE TABLE IF NOT EXISTS history_line_meta (
    row_id INTEGER PRIMARY KEY, author_id TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS replay_cursors (
    channel_id TEXT NOT NULL, user_id TEXT NOT NULL,
    ts TEXT NOT NULL, row_id INTEGER NOT NULL,
    PRIMARY KEY(channel_id, user_id)
);
"""

_store = SqliteStore("history_bm25.sqlite", _SCHEMA)


def _reset_db_path_for_test(path: str | None) -> None:
    """Test hook — pytest fixtures point the module at a tempfile."""
    _store.reset_path_for_test(path)


def _tokenize(text: str) -> str:
    try:
        from . import morpho
        tok = morpho.tokenize_for_index(text)
    except Exception as exc:
        log.warning("history morpho tokenize_for_index failed, raw fallback: %s", exc)
        tok = text
    return tok or (text or "").strip()


def index_line(
    channel_id: int, ts: str, role: str, text: str, author: str = "", *,
    author_id: str = "", source_id: str = "",
) -> bool:
    """Insert one conversation line into the FTS5 index.

    Exact redelivery retains its row ID. Different authors/message IDs
    remain separate even when timestamp, role and text are identical.
    ``author_id`` is a stable subject ID; display names never imply ownership.

    ``role`` is "user"/"assistant" for answered turns or "channel" for a
    message the app observed but did not reply to. ``author`` is the
    speaker's display name (mainly meaningful for "channel" rows, where the
    message can come from anyone in the channel).

    ``text`` is the verbatim line; it is stored in ``orig_text`` and ALSO
    morphologically tokenized into the indexed ``text`` column. Particles /
    fillers / 1-char noise are stripped by morpho.tokenize_for_index.
    """
    if channel_id is None or not text or not role:
        return False
    ts = ts or ""
    author = author or ""
    tokenized = _tokenize(text)

    def _insert(c) -> bool:
        if source_id:
            existing = c.execute(
                "SELECT h.rowid FROM history_fts h JOIN history_line_meta m ON m.row_id=h.rowid "
                "WHERE h.channel_id=? AND h.role=? AND m.author_id=? AND m.source_id=? LIMIT 1",
                (str(channel_id), role, str(author_id or ""), str(source_id)),
            ).fetchone()
            if existing:
                c.execute("UPDATE history_fts SET ts=?,author=?,orig_text=?,text=? WHERE rowid=?",
                          (ts, author, text, tokenized, existing[0]))
                return True
        existing = c.execute(
            "SELECT h.rowid FROM history_fts h LEFT JOIN history_line_meta m ON m.row_id=h.rowid "
            "WHERE h.channel_id=? AND h.ts=? AND h.role=? AND h.orig_text=? AND h.author=? "
            "AND COALESCE(m.author_id,'')=? AND COALESCE(m.source_id,'')=? LIMIT 1",
            (str(channel_id), ts, role, text, author, str(author_id or ""), str(source_id or "")),
        ).fetchone()
        if existing:
            return True
        cursor = c.execute(
            "INSERT INTO history_fts(channel_id, ts, role, author, orig_text, text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(channel_id), ts, role, author, text, tokenized),
        )
        c.execute("INSERT INTO history_line_meta(row_id,author_id,source_id) VALUES (?,?,?)",
                  (cursor.lastrowid, str(author_id or ""), str(source_id or "")))
        return True

    return bool(_store.write(_insert, what="index_line", default=False))


def search_history(query: str, channel_id: int, limit: int = 5) -> list[dict]:
    """BM25 keyword search over one channel's conversation history.

    Returns ``[{"role": str, "ts": str, "memory": str, "rank": int,
    "bm25_score": float}, ...]`` ordered by rank (1 = most relevant).

    The query goes through :func:`build_fts_match_query` (Janome content
    words when available, punctuation split fallback). Matching is
    exact-token (unicode61), not substring — the stored line was tokenized
    the same way, so "東洋水産" matches the standalone token indexed from a
    longer line.
    """
    q = (query or "").strip()
    if not q or channel_id is None:
        return []
    fts_query = build_fts_match_query(q)
    if not fts_query:
        return []

    def _search(c) -> list[dict]:
        rows = c.execute(
            "SELECT history_fts.rowid AS row_id, role, ts, author, orig_text, "
            "COALESCE(m.author_id,'') AS author_id, COALESCE(m.source_id,'') AS source_id, "
            "bm25(history_fts) AS score FROM history_fts "
            "LEFT JOIN history_line_meta m ON m.row_id=history_fts.rowid "
            "WHERE history_fts MATCH ? AND channel_id = ? "
            "ORDER BY bm25(history_fts) "
            "LIMIT ?",
            (fts_query, str(channel_id), max(int(limit), 1)),
        ).fetchall()
        return [
            {
                "role": r["role"],
                "ts": r["ts"],
                "author": r["author"] if "author" in r.keys() else "",
                "id": f"history:{channel_id}:{r['row_id']}",
                "author_id": r["author_id"], "source_id": r["source_id"],
                # Return orig_text verbatim, never the tokenized `text` column.
                "memory": r["orig_text"],
                "rank": i + 1,
                "bm25_score": float(r["score"]),
            }
            for i, r in enumerate(rows)
        ]

    return _store.read(_search, what="search_history", default=[]) or []


def get_lines_since(
    channel_id: int, since_ts: str = "", limit: int = 500, *, since_row_id: int | None = None,
) -> list[dict]:
    """Return raw conversation lines for one channel, oldest first, strictly
    after ``since_ts`` (lexical comparison — safe because lines are indexed
    with the ISO-8601-ish, zero-padded ``strftime`` format the JSONL writer
    uses).

    Used by ``replay.py``'s replay job to walk history in chronological
    order without depending on the source transcript files directly — this
    module already receives every line via ``index_line`` and the FTS5
    table holds each line's verbatim ``orig_text``, so replay can read
    entirely from here. Not a MATCH query (no keyword filter), so this is a
    plain indexed scan — fine for a nightly batch job, not meant for the hot
    search path.
    """
    if channel_id is None:
        return []

    def _fetch(c) -> list[dict]:
        clause = ""
        params: list = [str(channel_id)]
        if since_row_id is not None:
            clause = " AND (h.ts > ? OR (h.ts = ? AND h.rowid > ?))"
            params.extend([since_ts, since_ts, since_row_id])
        elif since_ts:
            clause = " AND h.ts > ?"
            params.append(since_ts)
        params.append(max(int(limit), 1))
        rows = c.execute(
            "SELECT h.rowid AS row_id, h.ts, h.role, h.author, h.orig_text, "
            "COALESCE(m.author_id,'') AS author_id, COALESCE(m.source_id,'') AS source_id "
            "FROM history_fts h LEFT JOIN history_line_meta m ON m.row_id=h.rowid "
            "WHERE h.channel_id=?" + clause + " ORDER BY h.ts ASC,h.rowid ASC LIMIT ?", params,
        ).fetchall()
        return [
            {"ts": r["ts"], "role": r["role"], "author": r["author"],
             "text": r["orig_text"], "row_id": r["row_id"],
             "author_id": r["author_id"], "source_id": r["source_id"]}
            for r in rows
        ]

    return _store.read(_fetch, what="get_lines_since", default=[]) or []


def get_replay_cursor(channel_id: int, user_id: str) -> tuple[str, int]:
    # Unlike an optional retrieval mirror, cursor failures must be visible.
    with _store.connect() as c:
        row = c.execute("SELECT ts,row_id FROM replay_cursors WHERE channel_id=? AND user_id=?",
                        (str(channel_id), user_id)).fetchone()
        return (row["ts"], row["row_id"]) if row else ("", 0)


def advance_replay_cursor(channel_id: int, user_id: str, ts: str, row_id: int) -> None:
    with _store.connect() as c, _store.lock:
        c.execute("""INSERT INTO replay_cursors VALUES (?,?,?,?)
            ON CONFLICT(channel_id,user_id) DO UPDATE SET ts=excluded.ts,row_id=excluded.row_id
            WHERE (excluded.ts,excluded.row_id) > (replay_cursors.ts,replay_cursors.row_id)""",
                  (str(channel_id), user_id, ts, row_id))
        c.commit()


def reindex_all(lines: Iterable[tuple]) -> int:
    """Transactionally replace indexed history; return the inserted row count.

    Rows are ``(channel_id, ts, role, text[, author[, author_id[, source_id]]])``.
    The existing 4/5-field forms remain supported; absent identity stays unknown,
    never inferred from a display name. Explicit identity fields survive rebuild.

    Prepare the iterable once before writing: a generator/tokenization failure
    leaves the old database untouched, and SQLite lock retries cannot consume an
    already-partially-used generator. DELETE and insertion share one transaction,
    so database failures preserve old rows, identity metadata and replay cursors.
    A successful rebuild resets cursors because regenerated row IDs cannot be
    compared with old cursors. This is a data rebuild, not a schema migration;
    changing the FTS tokenizer/schema requires a separate explicit migration.
    """
    prepared = []
    try:
        for row in lines:
            channel_id, ts, role, text = row[0], row[1], row[2], row[3]
            author = row[4] if len(row) > 4 else ""
            author_id = row[5] if len(row) > 5 else ""
            source_id = row[6] if len(row) > 6 else ""
            if channel_id is None or not text or not role:
                continue
            prepared.append((str(channel_id), ts or "", role, author or "", text,
                             _tokenize(text), str(author_id or ""), str(source_id or "")))
    except Exception as exc:
        log.warning("history reindex preparation failed; existing index retained: %s", exc)
        return 0

    def _insert_all(c) -> int:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM history_fts")
        c.execute("DELETE FROM history_line_meta")
        c.execute("DELETE FROM replay_cursors")
        for channel_id, ts, role, author, text, tokenized, author_id, source_id in prepared:
            cursor = c.execute(
                "INSERT INTO history_fts(channel_id, ts, role, author, orig_text, text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, ts, role, author, text, tokenized),
            )
            c.execute("INSERT INTO history_line_meta(row_id,author_id,source_id) VALUES (?,?,?)",
                      (cursor.lastrowid, author_id, source_id))
        return len(prepared)

    return _store.write(_insert_all, what="reindex_all", default=0) or 0


def stats() -> dict:
    total = _store.read(
        lambda c: c.execute("SELECT count(*) FROM history_fts").fetchone()[0],
        what="stats", default=-1,
    )
    return {"rows": total, "db_path": _store.resolve_path()}

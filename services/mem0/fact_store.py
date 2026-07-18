"""Structured fact layer (Mem0 v5 T8a) — attribute 付き fact の導出インデックス。

canonical ledgerを唯一の正本とし、このstoreはattribute付きfactだけの導出
projectionとして扱う。破損時はcanonical ledgerから再構築できる。

役割:
- 決定的 conflict ルートの照会を mem.get_all(全件+Pythonフィルタ) から
  SQL 索引付き lookup に置換（高速・堅牢）
- supersede 連鎖 (supersedes/superseded_by) と valid 期間で属性の履歴を保持
  (「なぜこの値になったか」を event-sourcing の最小形で追える)

接続・スキーマ管理は services/mem0/graph.py の確立パターンを踏襲:
STATE_DIR/mem0_facts.sqlite, WAL, _db_lock で write 直列化, CREATE TABLE IF NOT
EXISTS, 呼び出しは asyncio.to_thread 経由。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id                 TEXT PRIMARY KEY,   -- ChromaDB row id (正本への参照)
    user_id                 TEXT NOT NULL,
    attribute               TEXT NOT NULL,
    value_text              TEXT,               -- fact 本文 (表示・debug 用コピー)
    value_json              TEXT,               -- typed value (T7, JSON)
    condition               TEXT,               -- time:/context:/custom: or ''
    scope                   TEXT,               -- global/conversation:<id>/meta
    valid_from              INTEGER,
    valid_to                INTEGER,            -- NULL = 現在有効
    status                  TEXT NOT NULL DEFAULT 'active',  -- active/superseded
    source_conversation_id  TEXT,
    extractor_version       TEXT,
    supersedes              TEXT,               -- 置き換えた旧 fact_id
    superseded_by           TEXT,               -- これを置き換えた新 fact_id
    created_at              INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_user_attr
    ON facts(user_id, attribute, status);
CREATE INDEX IF NOT EXISTS idx_facts_user_valid
    ON facts(user_id, valid_to);
CREATE TABLE IF NOT EXISTS projection_receipts (
    event_id      TEXT PRIMARY KEY,
    processed_at  INTEGER NOT NULL
);
"""

_db_lock = threading.Lock()
_db_path: str | None = None
# Path for which journal_mode=WAL + schema have been applied. journal_mode is
# a persistent DB-level property whose lock is NOT governed by busy_timeout,
# so running `PRAGMA journal_mode=WAL` on every (concurrent) connection made
# ~30% of concurrent writes fail with "database is locked" — silently
# dropping derived-index rows (ADR-0007; same fix as history_index). We now
# apply it exactly once per DB file, serialized under _db_lock.
_wal_ready_path: str | None = None


def _reject_canonical_direct_mutation() -> None:
    from services.mem0._projection import reject_direct_mutation

    reject_direct_mutation("fact-store")


def _resolve_db_path() -> str:
    global _db_path
    if _db_path is None:
        try:
            from config import STATE_DIR
        except Exception:
            STATE_DIR = "/home/ubuntu/discord-bot/.state"
        os.makedirs(STATE_DIR, exist_ok=True)
        _db_path = os.path.join(STATE_DIR, "mem0_facts.sqlite")
    return _db_path


def _reset_for_tests(path: str | None = None) -> None:
    """テスト用: DB パスを差し替える（本番コードから呼ばない）。"""
    global _db_path, _wal_ready_path
    _db_path = path
    _wal_ready_path = None  # new DB file → re-init WAL + schema on next _conn()


@contextmanager
def _conn():
    """Open a connection with WAL, ensure schema, hand it out."""
    global _wal_ready_path
    path = _resolve_db_path()
    # check_same_thread=False — bot.py + scripts call from various threads
    # via asyncio.to_thread. _db_lock serializes writes; reads are safe.
    c = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=5000")
        if _wal_ready_path != path:
            with _db_lock:
                if _wal_ready_path != path:
                    c.execute("PRAGMA journal_mode=WAL")
                    c.executescript(_SCHEMA)
                    _wal_ready_path = path
        yield c
    finally:
        c.close()


def _row_to_dict(r: sqlite3.Row) -> dict:
    return dict(r)


def record_fact(
    fact_id: str,
    user_id: str,
    attribute: str,
    *,
    value_text: str = "",
    value_json: str | None = None,
    condition: str = "",
    scope: str = "",
    source_conversation_id: str | None = None,
    extractor_version: str | None = None,
    supersedes: str | None = None,
) -> bool:
    """attribute 付き fact を登録（INSERT OR REPLACE — 再 ingest に冪等）。

    supersedes を渡すと旧 fact 側の superseded_by / status も同時更新する。
    失敗は False（呼び元は best-effort、正本の Chroma insert は既に済んでいる）。
    """
    _reject_canonical_direct_mutation()
    if not fact_id or not user_id or not attribute:
        return False
    now = int(time.time())
    try:
        with _conn() as c, _db_lock:
            c.execute(
                """INSERT OR REPLACE INTO facts
                   (fact_id, user_id, attribute, value_text, value_json,
                    condition, scope, valid_from, valid_to, status,
                    source_conversation_id, extractor_version,
                    supersedes, superseded_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?,NULL,'active',?,?,?,NULL,?)""",
                (
                    fact_id,
                    user_id,
                    attribute,
                    value_text[:500],
                    value_json,
                    condition,
                    scope,
                    now,
                    source_conversation_id,
                    extractor_version,
                    supersedes,
                    now,
                ),
            )
            if supersedes:
                c.execute(
                    """UPDATE facts SET superseded_by=?, status='superseded',
                       valid_to=? WHERE fact_id=? AND superseded_by IS NULL""",
                    (fact_id, now, supersedes),
                )
            c.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 導出インデックスは本流を壊さない
        log.warning("fact_store record failed (non-fatal): %s", exc)
        return False


def supersede_fact(old_fact_id: str, new_fact_id: str | None = None) -> bool:
    """旧 fact を supersede 状態にする（Chroma 側 soft-archive と対で呼ぶ）。

    new_fact_id が None のときは「置き換え先不明の失効」（手動 forget 等）。
    """
    _reject_canonical_direct_mutation()
    if not old_fact_id:
        return False
    now = int(time.time())
    try:
        with _conn() as c, _db_lock:
            c.execute(
                """UPDATE facts SET status='superseded', valid_to=?,
                   superseded_by=COALESCE(?, superseded_by)
                   WHERE fact_id=?""",
                (now, new_fact_id, old_fact_id),
            )
            c.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("fact_store supersede failed (non-fatal): %s", exc)
        return False


def apply_projection(
    event_id: str,
    operation: str,
    fact_id: str,
    user_id: str,
    attribute: str,
    *,
    value_text: str = "",
    value_json: str | None = None,
    condition: str = "",
    scope: str = "",
    source_conversation_id: str | None = None,
    merged_into: str | None = None,
    _capability: object | None = None,
) -> bool:
    """Apply an outbox event and its receipt in one fact-store transaction."""
    from services.mem0._projection import require_internal_projection

    require_internal_projection(_capability, "fact-store")
    if not event_id or not fact_id:
        return False
    now = int(time.time())
    try:
        with _conn() as c, _db_lock:
            if c.execute(
                "SELECT 1 FROM projection_receipts WHERE event_id=?",
                (event_id,),
            ).fetchone():
                return True
            if operation in ("archive", "delete"):
                c.execute(
                    """UPDATE facts SET status='superseded',valid_to=?,
                       superseded_by=COALESCE(?,superseded_by) WHERE fact_id=?""",
                    (now, merged_into, fact_id),
                )
            elif attribute:
                c.execute(
                    """INSERT OR REPLACE INTO facts
                       (fact_id,user_id,attribute,value_text,value_json,condition,
                        scope,valid_from,valid_to,status,source_conversation_id,
                        extractor_version,supersedes,superseded_by,created_at)
                       VALUES (?,?,?,?,?,?,?, ?,NULL,'active',?,NULL,NULL,NULL,?)""",
                    (
                        fact_id,
                        user_id,
                        attribute,
                        value_text[:500],
                        value_json,
                        condition,
                        scope,
                        now,
                        source_conversation_id,
                        now,
                    ),
                )
            c.execute(
                "INSERT INTO projection_receipts(event_id,processed_at) VALUES (?,?)",
                (event_id, now),
            )
            c.commit()
        return True
    except Exception as exc:
        log.warning("fact_store projection failed for %s: %s", event_id, exc)
        return False


def get_active_facts(user_id: str, attribute: str) -> list[dict]:
    """user × attribute の現在有効な fact 行（決定ルートの照会用）。"""
    if not user_id or not attribute:
        return []
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT * FROM facts
                   WHERE user_id=? AND attribute=? AND status='active'
                   ORDER BY created_at DESC""",
                (user_id, attribute),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("fact_store get_active failed (non-fatal): %s", exc)
        return []


def get_history(user_id: str, attribute: str, limit: int = 50) -> list[dict]:
    """user × attribute の全履歴（supersede 連鎖含む、新しい順）。"""
    if not user_id or not attribute:
        return []
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT * FROM facts
                   WHERE user_id=? AND attribute=?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, attribute, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("fact_store get_history failed (non-fatal): %s", exc)
        return []


def count_rows() -> int:
    """weekly compact の突合チェック用（fact_store 側の総数）。"""
    try:
        with _conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM facts").fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0

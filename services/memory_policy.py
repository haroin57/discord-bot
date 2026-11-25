"""Authoritative explicit memory controls, independent of the mutable Mem0 backend.

Callers authenticate the Discord speaker and apply guild channel permissions first.
This layer only narrows visibility; it never grants access to another namespace.
SQLite is the commit point. Native contexts must carry both epoch and audience.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
SCOPES = {"shared", "private", "case", "legacy"}
MODES = {"wrong", "change", "cancel", "stop", "delete"}


def timestamp(value: Any = None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("時刻にはタイムゾーンを指定してください。")
        return value.timestamp()
    if isinstance(value, (float, int)):
        return float(value)
    if re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("時刻にはタイムゾーンを指定してください。")
    return parsed.timestamp()


def resolve_relative_dates(text: str, source_timestamp: Any) -> str:
    """Anchor each utterance independently, never to batch processing time.

    Week expressions remain ranges rather than inventing an appointment day.
    """
    day = datetime.fromtimestamp(timestamp(source_timestamp), JST).date()
    days = {"一昨日": -2, "昨日": -1, "今日": 0, "明日": 1, "明後日": 2}
    weeks = {"先週": -1, "今週": 0, "来週": 1, "再来週": 2}
    monday = day - timedelta(days=day.weekday())

    def replace(match):
        word = match.group()
        if word in days:
            return f"{day + timedelta(days=days[word])}（{word}）"
        start = monday + timedelta(weeks=weeks[word])
        return f"{start}〜{start + timedelta(days=6)}（{word}）"

    return re.sub("一昨日|明後日|再来週|昨日|今日|明日|先週|今週|来週", replace, text)


def _digest(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text)).casefold()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _ids(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = value
        value = parsed if isinstance(parsed, list) else [parsed]
    elif not isinstance(value, (list, tuple, set)):
        value = [value]
    return [str(item) for item in value if item is not None]


@dataclass(frozen=True)
class _MatchCandidate:
    record_id: str
    source_ids: frozenset[str]
    content: str
    content_hash: str

    @classmethod
    def from_row(cls, row):
        metadata = row.get("metadata") or {}
        source_ids = frozenset(
            _ids(row.get("source_message_ids")) + _ids(row.get("message_id"))
            + _ids(row.get("source_id")) + _ids(metadata.get("source_message_ids")))
        content = str(row.get("memory") or row.get("content") or row.get("text") or "")
        if not content:
            content = json.dumps(row, ensure_ascii=False, default=str)
        return cls(str(row.get("id") or ""), source_ids, content, _digest(content))

    def matches(self, ledger, source_ids):
        if self.record_id in {ledger["id"], ledger.get("external_id")}:
            return True
        if self.source_ids.intersection(source_ids):
            return True
        if self.content_hash in {ledger["content_hash"], ledger["quote_hash"]}:
            return True
        # Exact inclusion catches formatted context; opaque paraphrases need epochs.
        return any(ledger[key] and ledger[key] in self.content for key in ("text", "source_quote"))


@dataclass(frozen=True)
class ControlResult:
    status: str
    message: str
    record_ids: tuple[str, ...] = ()
    epoch: int = 0
    private_response: bool = False
    invalidated_ids: tuple[str, ...] = ()


class MemoryPolicy:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS policy_meta (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO policy_meta VALUES ('epoch', 0);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, external_id TEXT UNIQUE,
                    owner_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL, fact_kind TEXT NOT NULL,
                    text TEXT NOT NULL, source_quote TEXT NOT NULL,
                    content_hash TEXT NOT NULL, quote_hash TEXT NOT NULL,
                    source_channel_id TEXT, source_message_ids TEXT NOT NULL,
                    source_timestamp REAL NOT NULL, learned_at REAL NOT NULL,
                    valid_from REAL NOT NULL, valid_to REAL, original_valid_to REAL,
                    scope TEXT NOT NULL, case_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active', retired_at REAL,
                    successor_id TEXT, revision INTEGER NOT NULL DEFAULT 1,
                    deletion_state TEXT, attribute_key TEXT);
                CREATE INDEX IF NOT EXISTS memories_owner ON memories(owner_id);
                CREATE TABLE IF NOT EXISTS policy_events (
                    epoch INTEGER PRIMARY KEY, owner_id TEXT NOT NULL,
                    record_ids TEXT NOT NULL, source_message_ids TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at REAL NOT NULL,
                    control_source_message_id TEXT, source_timestamp REAL);
                CREATE TABLE IF NOT EXISTS preferences (
                    owner_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                    audience TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    source_message_id TEXT, source_timestamp REAL NOT NULL,
                    learned_at REAL NOT NULL, expires_at REAL,
                    PRIMARY KEY(owner_id, channel_id, audience, key));
                CREATE TABLE IF NOT EXISTS policy_due (
                    record_id TEXT NOT NULL, at REAL NOT NULL, owner_id TEXT NOT NULL,
                    fired INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(record_id, at));
                CREATE INDEX IF NOT EXISTS policy_due_pending ON policy_due(at) WHERE fired=0;
                CREATE TABLE IF NOT EXISTS memory_versions (
                    record_id TEXT NOT NULL, observed_from REAL NOT NULL,
                    observed_until REAL NOT NULL, snapshot TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS memory_versions_record ON memory_versions(record_id,observed_until);
                CREATE TABLE IF NOT EXISTS memory_links (
                    parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
                    PRIMARY KEY(parent_id,child_id));
                INSERT OR IGNORE INTO memory_links
                    SELECT id,successor_id FROM memories WHERE successor_id IS NOT NULL;
            """)
            event_columns = {row[1] for row in db.execute("PRAGMA table_info(policy_events)")}
            for name, kind in (("control_source_message_id", "TEXT"), ("source_timestamp", "REAL")):
                if name not in event_columns:
                    db.execute(f"ALTER TABLE policy_events ADD COLUMN {name} {kind}")

    @contextmanager
    def _connect(self, *, write: bool = False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA secure_delete=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def epoch(self) -> int:
        # Most checks are reads. Read revision and pending-boundary status from
        # one SQLite snapshot without reserving the database's writer lock.
        now = time.time()
        with self._connect() as db:
            revision, due = db.execute(
                "SELECT value, EXISTS(SELECT 1 FROM policy_due WHERE fired=0 AND at<=?) "
                "FROM policy_meta WHERE key='epoch'", (now,),
            ).fetchone()
            if not due:
                return int(revision)
        # Recheck under the write lock: another reader may have fired these
        # boundaries after our snapshot. Each event must invalidate only once.
        with self._connect(write=True) as db:
            due = db.execute("SELECT * FROM policy_due WHERE fired=0 AND at<=?", (time.time(),)).fetchall()
            for event in due:
                row = db.execute("SELECT * FROM memories WHERE id=?", (event["record_id"],)).fetchone()
                self._bump(db, event["owner_id"], [dict(row)] if row else [], "temporal")
                db.execute("UPDATE policy_due SET fired=1 WHERE record_id=? AND at=?", (event["record_id"], event["at"]))
            return int(db.execute("SELECT value FROM policy_meta WHERE key='epoch'").fetchone()[0])

    def is_current(self, epoch: int | None) -> bool:
        return epoch is not None and epoch == self.epoch()

    @staticmethod
    def audience_key(owner_id, channel_id=None, is_private=False) -> str:
        return json.dumps([str(owner_id), str(channel_id or ""), bool(is_private)], separators=(",", ":"))

    def can_reuse_context(self, stored_epoch, *, owner_id, channel_id=None,
                          is_private=False, stored_audience=None) -> bool:
        return self.is_current(stored_epoch) and stored_audience == self.audience_key(
            owner_id, channel_id, is_private)

    @staticmethod
    def _bump(db, owner_id, rows, reason, *, source_message_id=None, source_timestamp=None):
        db.execute("UPDATE policy_meta SET value=value+1 WHERE key='epoch'")
        epoch = db.execute("SELECT value FROM policy_meta WHERE key='epoch'").fetchone()[0]
        record_ids = sorted({str(row[key]) for row in rows for key in ("id", "external_id") if row.get(key)})
        source_ids = sorted({item for row in rows for item in _ids(row.get("source_message_ids"))})
        db.execute("INSERT INTO policy_events(epoch,owner_id,record_ids,source_message_ids,reason,created_at,control_source_message_id,source_timestamp) VALUES (?,?,?,?,?,?,?,?)", (
            epoch, str(owner_id), json.dumps(record_ids), json.dumps(source_ids), reason, time.time(),
            str(source_message_id) if source_message_id is not None else None,
            timestamp(source_timestamp) if source_timestamp is not None else None,
        ))
        return epoch, tuple(record_ids)

    def invalidations_since(self, epoch: int) -> list[dict]:
        with self._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM policy_events WHERE epoch>? ORDER BY epoch", (epoch,))]
        for row in rows:
            for key in ("record_ids", "source_message_ids"):
                row[key] = json.loads(row[key])
        return rows

    @staticmethod
    def _check_scope(scope, case_id):
        if scope not in SCOPES or (scope == "case" and not case_id):
            raise ValueError("共有範囲は shared/private/case と案件IDを指定してください。")

    def _insert(self, db, text, *, owner_id, channel_id=None, is_private=False,
                speaker_id=None, subject_id=None, fact_kind="fact", scope=None,
                case_id=None, source_message_id=None, source_message_ids=None,
                source_timestamp=None, valid_from=None, valid_to=None,
                external_id=None, attribute_key=None):
        if not text or not text.strip():
            raise ValueError("保存する内容がありません。")
        if len(text) > 12000:
            raise ValueError("記憶は12000文字以内で指定してください。")
        scope = scope or ("private" if is_private else "shared")
        self._check_scope(scope, case_id)
        if fact_kind not in {"fact", "plan", "hearsay", "inference"}:
            raise ValueError("記憶の種別が不正です。")
        src = timestamp(source_timestamp)
        start = timestamp(valid_from) if valid_from is not None else src
        end = timestamp(valid_to) if valid_to is not None else None
        if end is not None and end <= start:
            raise ValueError("有効終了は開始より後に指定してください。")
        row = dict(id="mp_" + uuid.uuid4().hex, external_id=external_id,
                   owner_id=str(owner_id), speaker_id=str(speaker_id or owner_id),
                   subject_id=str(subject_id or owner_id), fact_kind=fact_kind,
                   text=resolve_relative_dates(text.strip(), src), source_quote=text.strip(),
                   source_channel_id=str(channel_id) if channel_id is not None else None,
                   source_message_ids=json.dumps(sorted(set(_ids(source_message_ids) + _ids(source_message_id)))),
                   source_timestamp=src, learned_at=time.time(), valid_from=start, valid_to=end,
                   original_valid_to=end, scope=scope, case_id=case_id, status="active", revision=1,
                   attribute_key=attribute_key)
        row["content_hash"] = _digest(row["text"])
        row["quote_hash"] = _digest(row["source_quote"])
        columns = ",".join(row)
        db.execute(f"INSERT INTO memories ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        for boundary in (start, end):
            if boundary is not None and boundary > time.time():
                db.execute("INSERT OR IGNORE INTO policy_due(record_id,at,owner_id) VALUES (?,?,?)", (row["id"], boundary, str(owner_id)))
        return row

    def remember(self, text: str, owner_id, **kwargs) -> ControlResult:
        with self._connect(write=True) as db:
            # Idempotency is tied to the source message and exact instruction.
            source_id = kwargs.get("source_message_id")
            if source_id:
                for found in db.execute("SELECT * FROM memories WHERE owner_id=? AND quote_hash=?", (str(owner_id), _digest(text))):
                    if str(source_id) in _ids(found["source_message_ids"]):
                        active = found["status"] in {"active", "changed"}
                        return ControlResult("saved" if active else "inactive", "この発言の記憶は保存済みです。" if active else "この発言の記憶は失効済みです。再処理による復活は行いません。", (found["id"],), self._epoch_in(db), True)
            row = self._insert(db, text, owner_id=owner_id, **kwargs)
            epoch, _ = self._bump(db, owner_id, [row], "remember", source_message_id=source_id,
                                  source_timestamp=row["source_timestamp"])
        return ControlResult("saved", f"記憶を保存しました。ID: {row['id']}。共有範囲: {row['scope']}。", (row["id"],), epoch, row["scope"] == "private")

    @staticmethod
    def _epoch_in(db):
        return int(db.execute("SELECT value FROM policy_meta WHERE key='epoch'").fetchone()[0])

    @staticmethod
    def _get(db, owner_id, record_id):
        row = db.execute("SELECT * FROM memories WHERE owner_id=? AND (id=? OR external_id=?)",
                         (str(owner_id), str(record_id), str(record_id))).fetchone()
        if row is None:
            raise ValueError("本人の記憶IDが見つかりません。/recall で確認してください。")
        return dict(row)

    def get(self, owner_id, record_id) -> dict:
        with self._connect() as db:
            return self._get(db, owner_id, record_id)

    @staticmethod
    def _family(db, old):
        family = {old["id"]}
        rows = [dict(r) for r in db.execute("SELECT * FROM memories WHERE owner_id=?", (old["owner_id"],))]
        owned = {r["id"] for r in rows}
        links = [(r[0], r[1]) for r in db.execute("SELECT parent_id,child_id FROM memory_links")
                 if r[0] in owned and r[1] in owned]
        while True:
            more = {r["id"] for r in rows if r["id"] in family or r.get("successor_id") in family}
            more |= {r["successor_id"] for r in rows if r["id"] in family and r.get("successor_id")}
            more |= {endpoint for parent, child in links if parent in family or child in family
                     for endpoint in (parent, child)}
            if more <= family:
                return [r for r in rows if r["id"] in family]
            family |= more

    @staticmethod
    def _snapshot_before(db, row, at):
        last = db.execute("SELECT MAX(observed_until) FROM memory_versions WHERE record_id=?", (row["id"],)).fetchone()[0]
        db.execute("INSERT INTO memory_versions VALUES (?,?,?,?)", (
            row["id"], last if last is not None else row["learned_at"], at, json.dumps(row, ensure_ascii=False)))

    @staticmethod
    def _known_rows(db, rows, known_at):
        if known_at is None:
            return rows
        known = timestamp(known_at)
        selected = []
        for current in rows:
            # Privacy/use-stop/deletion applies now, even to questions about past beliefs.
            if current["status"] in {"stopped", "deleted"}:
                selected.append(current)
                continue
            if current["learned_at"] > known:
                continue
            old = db.execute("""SELECT snapshot FROM memory_versions WHERE record_id=?
                AND observed_from<=? AND observed_until>? ORDER BY observed_until LIMIT 1""",
                (current["id"], known, known)).fetchone()
            row = json.loads(old[0]) if old else current
            row["scope"], row["case_id"] = current["scope"], current["case_id"]
            selected.append(row)
        return selected

    def adopt_legacy(self, row: dict, owner_id) -> str:
        """Only call on rows already returned by an owner-scoped backend query."""
        meta = row.get("metadata") or {}
        claimed = row.get("user_id") or meta.get("user_id")
        if claimed is not None and str(claimed) != str(owner_id):
            raise ValueError("他者の記憶は変更できません。")
        if not row.get("id"):
            raise ValueError("記憶IDがありません。")
        with self._connect(write=True) as db:
            found = db.execute("SELECT id,owner_id FROM memories WHERE external_id=?", (str(row["id"]),)).fetchone()
            if found:
                if found["owner_id"] != str(owner_id):
                    raise ValueError("他者の記憶は変更できません。")
                return found["id"]
            inserted = self._insert(db, row.get("memory") or row.get("text") or "", owner_id=owner_id,
                external_id=str(row["id"]), scope="legacy", channel_id=meta.get("channel_id"),
                speaker_id=meta.get("source_author_id") or owner_id,
                subject_id=meta.get("subject_id") or owner_id,
                source_message_ids=meta.get("source_message_ids"),
                source_timestamp=meta.get("source_timestamp") or row.get("created_at") or time.time(),
                valid_from=meta.get("valid_from"), valid_to=meta.get("valid_to"))
            return inserted["id"]

    def correct(self, record_id: str, owner_id, *, mode="wrong", replacement=None,
                subject_id=None, speaker_id=None, fact_kind=None, source_message_id=None,
                source_timestamp=None, effective_at=None, **kwargs) -> ControlResult:
        if mode not in MODES:
            raise ValueError("訂正種別が不正です。")
        if mode in {"wrong", "change"} and not replacement:
            raise ValueError("訂正後の内容を指定してください。")
        with self._connect(write=True) as db:
            old = self._get(db, owner_id, record_id)
            if old["status"] == "deleted" and mode == "stop":
                return ControlResult("saved", "この記憶は削除済みで、参照も停止しています。", (old["id"],), self._epoch_in(db), True)
            if old["status"] not in {"active", "changed"} and mode not in {"delete", "stop"}:
                raise ValueError("失効済みの記憶です。現在の記憶IDを指定してください。")
            now = time.time()
            self._snapshot_before(db, old, now)
            effective = timestamp(effective_at if effective_at is not None else source_timestamp)
            new = None
            if mode in {"wrong", "change"}:
                new = self._insert(db, replacement, owner_id=owner_id,
                    channel_id=kwargs.get("channel_id", old["source_channel_id"]),
                    speaker_id=speaker_id or owner_id, subject_id=subject_id or old["subject_id"],
                    fact_kind=fact_kind or old["fact_kind"], scope=old["scope"], case_id=old["case_id"],
                    source_message_id=source_message_id, source_timestamp=source_timestamp,
                    valid_from=(effective if mode == "change" else old["valid_from"]),
                    valid_to=kwargs.get("valid_to", old["original_valid_to"]), attribute_key=old["attribute_key"])
                db.execute("INSERT OR IGNORE INTO memory_links VALUES (?,?)", (old["id"], new["id"]))
            status = {"wrong": "corrected", "change": "changed", "cancel": "cancelled", "stop": "stopped", "delete": "deleted"}[mode]
            end = min(old["valid_to"], effective) if old["valid_to"] is not None else effective
            db.execute("""UPDATE memories SET status=?,retired_at=?,successor_id=?,
                       valid_to=?,revision=revision+1,deletion_state=? WHERE id=?""", (
                status, now, new["id"] if new else old.get("successor_id"),
                end if mode in {"change", "cancel"} else old["valid_to"],
                "pending" if mode == "delete" else old.get("deletion_state"), old["id"]))
            affected = self._family(db, old) if mode in {"stop", "delete"} else [old]
            if mode == "cancel" and effective < old["valid_from"]:
                # Cancelling a future change must not expire the still-current fact
                # at the abandoned change date.
                predecessors = [dict(r) for r in db.execute(
                    "SELECT * FROM memories WHERE owner_id=? AND successor_id=? AND status='changed'",
                    (str(owner_id), old["id"]))]
                for previous in predecessors:
                    if previous["valid_to"] == old["valid_from"]:
                        self._snapshot_before(db, previous, now)
                        db.execute("UPDATE memories SET status='active',valid_to=original_valid_to,retired_at=NULL,revision=revision+1 WHERE id=?", (previous["id"],))
                        affected.append(previous)
            if mode in {"stop", "delete"}:
                for item in affected:
                    # A planned successor must not revive a stopped/deleted fact later.
                    state = "deleted" if mode == "delete" or item["status"] == "deleted" else "stopped"
                    db.execute("UPDATE memories SET status=?,retired_at=?,revision=revision+1 WHERE id=?", (state, now, item["id"]))
                    if mode == "delete":
                        db.execute("UPDATE memories SET text='',source_quote='',deletion_state='pending' WHERE id=?", (item["id"],))
                        db.execute("DELETE FROM memory_versions WHERE record_id=?", (item["id"],))
            if mode in {"change", "cancel"} and end > now:
                db.execute("INSERT OR IGNORE INTO policy_due(record_id,at,owner_id) VALUES (?,?,?)", (old["id"], end, str(owner_id)))
            epoch, invalidated = self._bump(db, owner_id, affected, mode,
                source_message_id=source_message_id, source_timestamp=source_timestamp)
        message = {"wrong": "元の記録を誤りとして訂正しました。", "change": "事実の変化と有効期間を保存しました。",
                   "cancel": "予定の取消を保存しました。", "stop": "今後の参照を停止しました。保存データは保持しています。",
                   "delete": "参照停止と台帳本文の削除を保存しました。その他の保存先は削除処理待ちです。"}[mode]
        return ControlResult("pending" if mode == "delete" else "saved", message,
                             (new["id"],) if new else (old["id"],), epoch, old["scope"] == "private", invalidated)

    def share(self, record_id, owner_id, scope, *, case_id=None,
              source_message_id=None, source_timestamp=None) -> ControlResult:
        self._check_scope(scope, case_id)
        if scope == "legacy":
            raise ValueError("変更後の共有範囲を指定してください。")
        with self._connect(write=True) as db:
            old = self._get(db, owner_id, record_id)
            if old["status"] in {"deleted", "stopped"}:
                raise ValueError("使用停止済みの記憶の共有範囲は変更できません。")
            # Apply to the entire known successor chain to avoid a historical leak.
            affected = self._family(db, old)
            for item in affected:
                db.execute("UPDATE memories SET scope=?,case_id=?,revision=revision+1 WHERE id=?", (scope, case_id, item["id"]))
            epoch, invalidated = self._bump(db, owner_id, affected, "share",
                source_message_id=source_message_id, source_timestamp=source_timestamp)
        return ControlResult("saved", f"共有範囲を {scope} に変更しました。既存投稿の削除は別の操作です。", (old["id"],), epoch, scope == "private", invalidated)

    @staticmethod
    def _visible(row, owner_id, is_private, case_id):
        if row["scope"] == "private":
            return bool(is_private) and row["owner_id"] == str(owner_id)
        if row["scope"] == "case":
            return bool(case_id) and str(case_id) == row["case_id"]
        return True

    @staticmethod
    def _active(row, as_of=None, known_at=None):
        when = timestamp(as_of)
        known = timestamp(known_at) if known_at is not None else None
        if known is not None and row["learned_at"] > known:
            return False
        if row["status"] in {"stopped", "deleted"}:
            return False
        believed_before = known is not None and row.get("retired_at") is not None and known < row["retired_at"]
        if row["status"] == "corrected" and not believed_before:
            return False
        end = row["original_valid_to"] if believed_before else row["valid_to"]
        return row["valid_from"] <= when and (end is None or when < end)

    def recall(self, owner_id, channel_id=None, is_private=False, case_id=None,
               as_of=None, known_at=None, query="", include_inactive=False,
               inspection=False) -> list[dict]:
        with self._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM memories WHERE owner_id=? ORDER BY learned_at DESC,id", (str(owner_id),))]
            rows = self._known_rows(db, rows, known_at)
        return [self._public_row(row, known_at) for row in rows
                if ((inspection and is_private) or self._visible(row, owner_id, is_private, case_id))
                and (known_at is None or row["learned_at"] <= timestamp(known_at))
                and (self._active(row, as_of, known_at) or (include_inactive and row["status"] != "deleted"))
                and (not query or query.casefold() in row["text"].casefold() or query == row["id"])]

    def inspect_records(self, owner_id, *, query="", include_inactive=False):
        """Owner-only ephemeral inspection, including not-yet-effective and case facts."""
        with self._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM memories WHERE owner_id=? ORDER BY learned_at DESC,id", (str(owner_id),))]
        return [self._public_row(row) for row in rows if row["status"] != "deleted"
                and (include_inactive or row["status"] in {"active", "changed"})
                and (not query or query.casefold() in row["text"].casefold() or query == row["id"])]

    @staticmethod
    def _public_row(row, known_at=None):
        metadata = {key: row.get(key) for key in (
            "owner_id", "speaker_id", "subject_id", "fact_kind", "scope", "case_id",
            "source_channel_id", "source_timestamp", "learned_at", "valid_from", "valid_to", "status", "revision")}
        metadata["source_message_ids"] = _ids(row["source_message_ids"])
        metadata["source_quote"] = row["source_quote"]
        metadata["source"] = "legacy_mem0" if row.get("external_id") else "explicit_user"
        if known_at is not None:
            metadata["known_at"] = timestamp(known_at)
            metadata["knowledge_state"] = "believed_then"
        # Legacy formatters often retain only `memory`. Keep attribution in that field
        # as well so another person's fact cannot silently become the owner's fact.
        content = row["text"]
        if row["subject_id"] != row["owner_id"] or row["fact_kind"] in {"hearsay", "inference", "plan"}:
            content = f"【対象:{row['subject_id']} / 話者:{row['speaker_id']} / 種別:{row['fact_kind']}】{content}"
        if known_at is not None:
            content = "【当時把握していた説明。現在の真偽とは別です。】" + content
        return {"id": row["id"], "memory": content, "metadata": metadata, "user_id": row["owner_id"]}

    def _all(self, known_at=None):
        with self._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM memories")]
            return self._known_rows(db, rows, known_at)

    @staticmethod
    def _matches(candidate, ledger):
        return _MatchCandidate.from_row(candidate).matches(ledger, _ids(ledger["source_message_ids"]))

    def filter_memories(self, rows, owner_id, channel_id=None, is_private=False,
                        case_id=None, as_of=None, known_at=None) -> list[dict]:
        ledger = self._all(known_at)
        canonical_by_id = {row["id"]: row for row in ledger}
        prepared_ledger = [(row, frozenset(_ids(row["source_message_ids"]))) for row in ledger]
        output = []
        for candidate in rows:
            metadata = candidate.get("metadata") or {}
            claimed = candidate.get("user_id") or metadata.get("user_id") or metadata.get("owner_id")
            if claimed is not None and str(claimed) != str(owner_id):
                continue
            if metadata.get("scope") == "private" and not is_private:
                continue
            if metadata.get("scope") == "case" and (not case_id or metadata.get("case_id") != case_id):
                continue
            canonical = canonical_by_id.get(str(candidate.get("id")))
            if canonical is not None:
                matches = [canonical]
            elif prepared_ledger:
                prepared = _MatchCandidate.from_row(candidate)
                matches = [r for r, source_ids in prepared_ledger if prepared.matches(r, source_ids)]
            else:
                matches = []
            if any(not self._visible(r, owner_id, is_private, case_id) or not self._active(r, as_of, known_at) for r in matches):
                continue
            if not matches:
                when = timestamp(as_of)
                try:
                    if metadata.get("valid_from") is not None and timestamp(metadata["valid_from"]) > when:
                        continue
                    if metadata.get("valid_to") is not None and timestamp(metadata["valid_to"]) <= when:
                        continue
                    learned = metadata.get("learned_at") or candidate.get("created_at")
                    if known_at is not None and (learned is None or timestamp(learned) > timestamp(known_at)):
                        continue
                except (TypeError, ValueError):
                    continue
            output.append(candidate)
        return output

    def filter_context_rows(self, rows, owner_id, channel_id=None, is_private=False, case_id=None) -> list[dict]:
        ledger = self._all()
        restricted = [(r, frozenset(_ids(r["source_message_ids"]))) for r in ledger
                      if not self._visible(r, owner_id, is_private, case_id) or not self._active(r)]
        output = []
        for row in rows:
            # Unlike memory retrieval, a permission-checked guild row may have another author.
            if restricted:
                prepared = _MatchCandidate.from_row(row)
                if any(prepared.matches(r, source_ids) for r, source_ids in restricted):
                    continue
            output.append(row)
        return output

    def sanitize_context(self, text, owner_id, channel_id=None, is_private=False,
                         context_epoch=None, case_id=None) -> str:
        if context_epoch is not None and not self.is_current(context_epoch):
            return ""
        restricted = [r for r in self._all() if not self._visible(r, owner_id, is_private, case_id) or not self._active(r)]
        # An unversioned summary cannot prove that it did not paraphrase revoked facts.
        if context_epoch is None and restricted:
            return ""
        if restricted:
            prepared = _MatchCandidate.from_row({"content": text})
            if any(prepared.matches(r, _ids(r["source_message_ids"])) for r in restricted):
                return ""
        return text

    async def delete(self, record_id, owner_id, *, backend_delete=None,
                     source_message_id=None, source_timestamp=None) -> ControlResult:
        with self._connect() as db:
            old = self._get(db, owner_id, record_id)
            family = self._family(db, old)
        result = self.correct(record_id, owner_id, mode="delete", source_message_id=source_message_id,
                              source_timestamp=source_timestamp)
        states = []
        for item in family:
            state = "local_deleted"
            if item.get("external_id"):
                if backend_delete is None:
                    from services.mem0 import delete_memory
                    backend_delete = delete_memory
                try:
                    ok = await backend_delete(item["external_id"])
                except Exception:
                    ok = False
                state = "backend_deleted" if ok else "pending"
            states.append(state)
            with self._connect(write=True) as db:
                db.execute("UPDATE memories SET deletion_state=? WHERE id=?", (state, item["id"]))
        with self._connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if "pending" in states:
            message = "台帳本文を削除し参照を停止しました。Mem0の削除は処理待ちです。/forget で同じIDを指定すると再試行します。"
        else:
            message = "訂正履歴を含む台帳本文を削除しました。" + ("Mem0の対象レコードも削除しました。" if "backend_deleted" in states else "")
            message += "Discord投稿・会話ログ・索引・バックアップ等の全保存先削除は完了していません。"
        return ControlResult("pending" if "pending" in states else "partial_deleted", message,
                             tuple(item["id"] for item in family), result.epoch, True, result.invalidated_ids)

    def set_preference(self, owner_id, key, value, *, channel_id=None, audience="all",
                       source_message_id=None, source_timestamp=None, expires_at=None) -> ControlResult:
        from services.persona_policy import validate_preference
        validate_preference(key, value)
        if audience not in {"all", "private", "public"}:
            raise ValueError("適用先は all/private/public で指定してください。")
        with self._connect(write=True) as db:
            db.execute("""INSERT INTO preferences VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(owner_id,channel_id,audience,key) DO UPDATE SET
                       value=excluded.value,source_message_id=excluded.source_message_id,
                       source_timestamp=excluded.source_timestamp,learned_at=excluded.learned_at,
                       expires_at=excluded.expires_at""", (
                str(owner_id), str(channel_id or ""), audience, key, value,
                str(source_message_id) if source_message_id is not None else None,
                timestamp(source_timestamp), time.time(), timestamp(expires_at) if expires_at is not None else None))
            epoch, _ = self._bump(db, owner_id, [], "preference",
                source_message_id=source_message_id, source_timestamp=source_timestamp)
            if expires_at is not None and timestamp(expires_at) > time.time():
                preference_id = "preference:" + _digest(json.dumps([str(owner_id), channel_id, audience, key]))
                db.execute("INSERT OR IGNORE INTO policy_due(record_id,at,owner_id) VALUES (?,?,?)", (preference_id, timestamp(expires_at), str(owner_id)))
        return ControlResult("saved", "応答の好みと適用先を保存しました。", epoch=epoch, private_response=True)

    def preferences(self, owner_id, channel_id=None, is_private=False) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute("""SELECT * FROM preferences WHERE owner_id=?
                AND channel_id IN ('',?) AND audience IN ('all',?)
                AND (expires_at IS NULL OR expires_at>?)
                ORDER BY (channel_id!=''),(audience!='all'),learned_at""", (
                str(owner_id), str(channel_id or ""), "private" if is_private else "public", time.time())).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def clear_preference(self, owner_id, key, *, channel_id=None, audience="all") -> ControlResult:
        with self._connect(write=True) as db:
            db.execute("DELETE FROM preferences WHERE owner_id=? AND channel_id=? AND audience=? AND key=?",
                       (str(owner_id), str(channel_id or ""), audience, key))
            epoch, _ = self._bump(db, owner_id, [], "preference")
        return ControlResult("saved", "指定した応答の好みを解除しました。", epoch=epoch, private_response=True)

    def _target(self, owner_id, target):
        target = target.strip().strip("「」『』\" ")
        with self._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM memories WHERE owner_id=? AND status IN ('active','changed')", (str(owner_id),))]
        direct = [r for r in rows if target in {r["id"], r.get("external_id")}]
        if direct:
            return direct[0]["id"]
        exact = [r for r in rows if target in {r["text"], r["source_quote"]}]
        return exact[0]["id"] if len(exact) == 1 else None

    async def handle_control(self, text, owner_id, channel_id=None, is_private=False,
                             source_message_id=None, source_timestamp=None):
        """Conservative explicit controls; never infer targets from model context.

        Natural language callers must check their existing admin/admission policy.
        Slash commands expose all structured variants including historical times.
        """
        text = text.strip().rstrip("。")
        common = dict(channel_id=channel_id, is_private=is_private,
                      source_message_id=source_message_id, source_timestamp=source_timestamp)
        try:
            from services.persona_policy import parse_preference
            preference = parse_preference(text, source_timestamp)
            if preference:
                key, value, local, expiry = preference
                return self.set_preference(owner_id, key, value, channel_id=channel_id if local else None,
                    audience="private" if is_private else "public", source_message_id=source_message_id,
                    source_timestamp=source_timestamp, expires_at=expiry)
            if text in {"どう覚えている", "どう覚えているか", "どう覚えているか教えて", "記憶を見せて"}:
                # Source details are a private inspection operation.
                return ControlResult("private_required", "/recall で本人だけに記憶内容・出典・共有範囲を表示します。", epoch=self.epoch(), private_response=True)
            match = re.fullmatch(r"(?:(本人限定|共有可)で)?覚えて[：:\s]+(.+)", text, re.S)
            if not match:
                match = re.fullmatch(r"(.+?)(?:を|と)(?:(本人限定|共有可)で)?覚えて(?:おいて)?", text, re.S)
                if match:
                    content, scope_word = match.groups()
                else:
                    content, scope_word = None, None
            else:
                scope_word, content = match.groups()
            if content:
                # An explicitly named relative is a subject, never the speaker's own attribute.
                subject = re.match(r"^(父|母|夫|妻|兄|姉|弟|妹|友人)(?:は|の)", content)
                return self.remember(content.strip("「」"), owner_id,
                    scope={"本人限定": "private", "共有可": "shared"}.get(scope_word),
                    subject_id=f"{owner_id}:{subject.group(1)}" if subject else None,
                    fact_kind=("hearsay" if any(word in content for word in ("と聞いた", "と言っていた", "とのこと"))
                               else "plan" if "予定" in content else "fact"), **common)
            match = re.fullmatch(r"(?:訂正|元から誤り|実際の変化|変更)[：:\s]+(.+?)\s*(?:→|=>)\s*(.+)", text, re.S)
            if match:
                target = self._target(owner_id, match.group(1))
                if target:
                    replacement = match.group(2)
                    subject = re.match(r"^(父|母|夫|妻|兄|姉|弟|妹|友人)(?:は|の)", replacement)
                    return self.correct(target, owner_id, replacement=replacement,
                        mode="change" if text.startswith(("実際の変化", "変更")) else "wrong",
                        subject_id=f"{owner_id}:{subject.group(1)}" if subject else None,
                        fact_kind="hearsay" if any(word in replacement for word in ("と聞いた", "と言っていた", "とのこと")) else None, **common)
                return ControlResult("needs_target", "訂正対象を一意に特定できません。/recall の記憶IDと訂正後の内容を指定してください。", epoch=self.epoch())
            match = re.fullmatch(r"(.+?)を(本人限定|共有可|使用停止|物理削除|予定取消)(?:にして|して|に)?", text, re.S)
            if match:
                target = self._target(owner_id, match.group(1))
                if not target:
                    return ControlResult("needs_target", "対象を一意に特定できません。/recall の記憶IDを指定してください。", epoch=self.epoch())
                action = match.group(2)
                if action in {"本人限定", "共有可"}:
                    return self.share(target, owner_id, "private" if action == "本人限定" else "shared",
                        source_message_id=source_message_id, source_timestamp=source_timestamp)
                if action == "物理削除":
                    return await self.delete(target, owner_id, source_message_id=source_message_id, source_timestamp=source_timestamp)
                return self.correct(target, owner_id, mode="stop" if action == "使用停止" else "cancel", **common)
            if re.fullmatch(r".+(?:を忘れて|は忘れて|は今後出さないで|の予定をやめた|と訂正して)", text, re.S) or text in {"覚えて", "訂正して", "忘れて"}:
                return ControlResult("needs_target", "対象の記憶IDを /recall で確認し、/correction で誤り・変化・取消・使用停止、/forget で保存先の削除を指定してください。", epoch=self.epoch())
            if re.fullmatch(r".+(?:私ではなく.+の話|予定だったが(?:中止|取消)(?:に)?した)", text, re.S):
                return ControlResult("needs_target", "対象の記憶IDを /recall で確認し、/correction で対象人物の訂正または予定取消を指定してください。", epoch=self.epoch())
        except ValueError as exc:
            return ControlResult("invalid", str(exc), epoch=self.epoch(), private_response=True)
        except sqlite3.Error:
            return ControlResult("failed", "記憶操作の完了を確認できません。/recall で状態を確認し、必要なら再試行してください。", private_response=True)
        return None


_instances: dict[str, MemoryPolicy] = {}


def get_memory_policy() -> MemoryPolicy:
    from config import STATE_DIR
    path = str(Path(STATE_DIR) / "memory_policy.sqlite3")
    if path not in _instances:
        _instances[path] = MemoryPolicy(path)
    return _instances[path]


get_policy = get_memory_policy


async def handle_control(text, owner_id, channel_id=None, is_private=False,
                         source_message_id=None, source_timestamp=None) -> ControlResult | None:
    return await get_memory_policy().handle_control(text, owner_id, channel_id, is_private,
                                                   source_message_id, source_timestamp)

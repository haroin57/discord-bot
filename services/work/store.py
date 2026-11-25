"""SQLite source of truth for work, separate from model sessions and Discord tokens.

All mutating operations commit before returning. Methods join a transaction opened
on this instance in the same thread; reads outside it never acquire a write lock,
so an external effects connection can inspect a revision while claiming an effect.
Do not retain a transaction across an await or perform network I/O inside it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class WorkError(Exception):
    """Expected domain or persistence error, safe to present to the requester."""

    code = "work_error"


class WorkNotFound(WorkError):
    code = "not_found"


class WorkAccessDenied(WorkError):
    code = "access_denied"


class WorkAmbiguous(WorkError):
    code = "ambiguous"


class WorkRevisionConflict(WorkError):
    code = "revision_conflict"


class WorkValidationError(WorkError):
    code = "invalid_input"


class WorkStateConflict(WorkError):
    code = "state_conflict"


class WorkStorageError(WorkError):
    code = "storage_error"


class WorkIdempotencyConflict(WorkError):
    code = "idempotency_conflict"


STATES = frozenset({
    "queued", "running", "waiting", "paused", "completed", "cancelled",
    "needs_reconciliation",
})
TERMINAL_STATES = frozenset({"completed", "cancelled"})
AUDIENCES = frozenset({"shared", "owner", "case", "channel"})
_TRANSITIONS = {
    "queued": {"running", "waiting", "paused", "cancelled", "needs_reconciliation"},
    "running": {"queued", "waiting", "paused", "completed", "cancelled", "needs_reconciliation"},
    "waiting": {"queued", "running", "paused", "cancelled", "completed", "needs_reconciliation"},
    "paused": {"queued", "running", "cancelled", "needs_reconciliation"},
    "needs_reconciliation": {"paused", "cancelled"},
    "completed": {"queued"},
    "cancelled": set(),
}
_DICT_FIELDS = {"requirements", "preferences"}
_LIST_FIELDS = {"decisions", "progress", "unknowns", "completion_criteria", "participants"}
_EDIT_FIELDS = _DICT_FIELDS | _LIST_FIELDS | {"goal", "next_action", "audience"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkValidationError("保存する値はJSON互換の形式で指定してください。") from exc


def _string(value: Any, name: str) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WorkValidationError(f"{name}を指定してください。")
    result = str(value).strip()
    if not result:
        raise WorkValidationError(f"{name}を指定してください。")
    return result


@dataclass(frozen=True)
class Work:
    id: str
    owner_id: str
    channel_id: str
    guild_id: str | None
    goal: str
    revision: int = 1
    state: str = "queued"
    requirements: dict = field(default_factory=dict)
    decisions: list = field(default_factory=list)
    progress: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)
    next_action: str = ""
    completion_criteria: list = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    audience: str = "shared"
    preferences: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def snapshot(self) -> dict:
        return asdict(self)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel_id TEXT NOT NULL,
    state TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS works_channel_state ON works(channel_id, state);
CREATE TABLE IF NOT EXISTS work_requests (
    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    work_id TEXT NOT NULL REFERENCES works(id), result_id TEXT
);
CREATE TABLE IF NOT EXISTS work_messages (
    message_id TEXT NOT NULL, channel_id TEXT NOT NULL,
    work_id TEXT NOT NULL REFERENCES works(id), PRIMARY KEY(message_id, channel_id)
);
CREATE INDEX IF NOT EXISTS work_messages_channel_work ON work_messages(channel_id, work_id);
CREATE TABLE IF NOT EXISTS work_questions (
    id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS questions_work ON work_questions(work_id);
CREATE TABLE IF NOT EXISTS work_artifacts (
    id TEXT NOT NULL, work_id TEXT NOT NULL REFERENCES works(id),
    source_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(id, version), UNIQUE(work_id, source_id, version)
);
CREATE TABLE IF NOT EXISTS work_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, work_id TEXT NOT NULL REFERENCES works(id),
    kind TEXT NOT NULL, payload TEXT NOT NULL, actor_id TEXT,
    revision INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_work ON work_events(work_id, id);
CREATE TABLE IF NOT EXISTS work_checkpoints (
    work_id TEXT NOT NULL REFERENCES works(id), kind TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(work_id, kind)
);
"""


class WorkStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._local = threading.local()
        self._uri = str(path) == ":memory:"
        self._database = f"file:yui-work-{uuid.uuid4().hex}?mode=memory&cache=shared" if self._uri else str(self.path)
        self._anchor = None
        try:
            if not self._uri:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            if self._uri:
                self._anchor = conn
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
            finally:
                if not self._uri:
                    conn.close()
        except (sqlite3.Error, OSError) as exc:
            raise WorkStorageError("用件の保存先を初期化できませんでした。") from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._database, timeout=10, isolation_level=None, uri=self._uri)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        conn = active
        try:
            if conn is None:
                conn = self._connect()
            yield conn
        except sqlite3.Error as exc:
            raise WorkStorageError("用件の記録を読み取れませんでした。") from exc
        finally:
            if active is None and conn is not None:
                conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        conn = active
        savepoint = "s" + uuid.uuid4().hex
        try:
            if conn is None:
                conn = self._connect()
                conn.execute("BEGIN IMMEDIATE")
                self._local.connection = conn
            else:
                conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield conn
                conn.execute(f"RELEASE SAVEPOINT {savepoint}" if active is not None else "COMMIT")
            except BaseException:
                if active is not None:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                elif conn.in_transaction:
                    conn.rollback()
                raise
        except sqlite3.Error as exc:
            raise WorkStorageError("用件の変更を保存できませんでした。") from exc
        finally:
            if active is None:
                self._local.connection = None
                if conn is not None:
                    conn.close()

    @staticmethod
    def _authorize(work: Work, actor_id: str | None) -> None:
        if actor_id is None:
            return
        actor = str(actor_id)
        if actor == work.owner_id:
            return
        if work.audience != "owner" and actor in work.participants:
            return
        raise WorkAccessDenied("この用件を参照・変更する権限がありません。")

    def get(self, work_id: str, actor_id: str | None = None) -> Work:
        with self._read() as conn:
            row = conn.execute("SELECT data FROM works WHERE id=?", (str(work_id),)).fetchone()
            if row is None:
                raise WorkNotFound("用件IDに対応する記録がありません。")
            work = Work(**json.loads(row["data"]))
            self._authorize(work, actor_id)
            return work

    def assert_current(self, work_id: str, expected_revision: int, actor_id: str | None = None,
                       *, allowed_states: set[str] | None = None) -> Work:
        work = self.get(work_id, actor_id)
        if isinstance(expected_revision, bool) or work.revision != expected_revision:
            raise WorkRevisionConflict("用件の条件または状態が更新されています。最新の版を確認してください。")
        if allowed_states is not None and work.state not in allowed_states:
            raise WorkStateConflict("現在の用件状態ではこの操作を開始できません。")
        return work

    def _check(self, work_id: str, actor_id: str | None, expected_revision: int | None) -> Work:
        return self.get(work_id, actor_id) if expected_revision is None else self.assert_current(work_id, expected_revision, actor_id)

    @staticmethod
    def _fields(fields: dict) -> dict:
        unexpected = set(fields) - _EDIT_FIELDS
        if unexpected:
            raise WorkValidationError("変更できないフィールドが指定されています。")
        values = json.loads(_json(fields))
        for name in _DICT_FIELDS & values.keys():
            if not isinstance(values[name], dict):
                raise WorkValidationError(f"{name}は辞書で指定してください。")
        for name in _LIST_FIELDS & values.keys():
            if not isinstance(values[name], list):
                raise WorkValidationError(f"{name}はリストで指定してください。")
        if "participants" in values:
            values["participants"] = list(dict.fromkeys(_string(p, "参加者ID") for p in values["participants"]))
        if "audience" in values and (not isinstance(values["audience"], str) or values["audience"] not in AUDIENCES):
            raise WorkValidationError("共有範囲が不正です。")
        if "goal" in values:
            values["goal"] = _string(values["goal"], "目的")
        if "next_action" in values and not isinstance(values["next_action"], str):
            raise WorkValidationError("次の行動は文字列で指定してください。")
        return values

    @staticmethod
    def _fingerprint(kind: str, payload: Any) -> str:
        return hashlib.sha256(_json([kind, payload]).encode()).hexdigest()

    def _request(self, conn, request_id, kind, payload):
        if request_id is None:
            return None, None
        request_id = _string(request_id, "request_id")
        fingerprint = self._fingerprint(kind, payload)
        row = conn.execute("SELECT * FROM work_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is not None and row["fingerprint"] != fingerprint:
            raise WorkIdempotencyConflict("同じ受付IDに異なる操作が指定されています。")
        return row, fingerprint

    @staticmethod
    def _save_request(conn, request_id, fingerprint, work_id, result_id=None):
        if request_id is not None:
            conn.execute("INSERT INTO work_requests VALUES (?,?,?,?)", (str(request_id).strip(), fingerprint, work_id, result_id))

    @staticmethod
    def _save(conn, before: Work, data: dict) -> Work:
        data["revision"] = before.revision + 1
        data["updated_at"] = _now()
        work = Work(**data)
        changed = conn.execute(
            "UPDATE works SET state=?, revision=?, data=? WHERE id=? AND revision=?",
            (work.state, work.revision, _json(data), work.id, before.revision),
        ).rowcount
        if changed != 1:
            raise WorkRevisionConflict("用件は別の操作で更新されています。")
        return work

    @staticmethod
    def _event(conn, work: Work, kind: str, payload: Any, actor_id=None) -> dict:
        created_at = _now()
        encoded = _json(payload if payload is not None else {})
        cursor = conn.execute(
            "INSERT INTO work_events(work_id,kind,payload,actor_id,revision,created_at) VALUES (?,?,?,?,?,?)",
            (work.id, kind, encoded, None if actor_id is None else str(actor_id), work.revision, created_at),
        )
        return dict(id=cursor.lastrowid, work_id=work.id, kind=kind, payload=json.loads(encoded),
                    actor_id=None if actor_id is None else str(actor_id), revision=work.revision, created_at=created_at)

    def create(self, owner_id, channel_id, goal, request_id, guild_id=None, **fields) -> Work:
        owner_id, channel_id = _string(owner_id, "依頼者ID"), _string(channel_id, "チャンネルID")
        goal, request_id = _string(goal, "目的"), _string(request_id, "request_id")
        guild_id = None if guild_id is None else _string(guild_id, "サーバーID")
        values = self._fields(fields)
        payload = [owner_id, channel_id, goal, guild_id, values]
        with self.transaction() as conn:
            previous, fingerprint = self._request(conn, request_id, "create", payload)
            if previous is not None:
                return self.get(previous["work_id"], owner_id)
            now = _now()
            work = Work(id=_id("w"), owner_id=owner_id, channel_id=channel_id, goal=goal,
                        guild_id=guild_id, created_at=now, updated_at=now, **values)
            conn.execute("INSERT INTO works VALUES (?,?,?,?,?,?)", (work.id, work.owner_id, work.channel_id,
                         work.state, work.revision, _json(work.snapshot())))
            self._save_request(conn, request_id, fingerprint, work.id)
            self._event(conn, work, "accepted", {"goal": goal}, owner_id)
            return work

    def list_for(self, actor_id, channel_id=None, active_only=True) -> list[Work]:
        actor_id = _string(actor_id, "利用者ID")
        with self._read() as conn:
            query = "SELECT data FROM works"
            parameters = ()
            if channel_id is not None:
                query += (
                    " WHERE works.channel_id=? OR EXISTS ("
                    "SELECT 1 FROM work_messages WHERE work_messages.work_id=works.id "
                    "AND work_messages.channel_id=?)"
                )
                parameters = (str(channel_id), str(channel_id))
            rows = conn.execute(query, parameters).fetchall()
            result = []
            for row in rows:
                work = Work(**json.loads(row["data"]))
                if active_only and work.state in TERMINAL_STATES:
                    continue
                try:
                    self._authorize(work, actor_id)
                except WorkAccessDenied:
                    continue
                result.append(work)
            return sorted(result, key=lambda w: (w.updated_at, w.id), reverse=True)

    def _invalidate(self, conn, work_id: str, reason: str) -> int:
        count = 0
        for row in conn.execute("SELECT id,version,data FROM work_artifacts WHERE work_id=?", (work_id,)).fetchall():
            data = json.loads(row["data"])
            if not data["invalidated"]:
                data.update(invalidated=True, invalidation_reason=reason)
                conn.execute("UPDATE work_artifacts SET data=? WHERE id=? AND version=?", (_json(data), row["id"], row["version"]))
                count += 1
        for row in conn.execute("SELECT kind,data FROM work_checkpoints WHERE work_id=?", (work_id,)).fetchall():
            data = json.loads(row["data"])
            data["invalidated"] = True
            conn.execute("UPDATE work_checkpoints SET data=? WHERE work_id=? AND kind=?", (_json(data), work_id, row["kind"]))
        return count

    def amend(self, work_id, actor_id, text, request_id, **fields) -> Work:
        request_id = _string(request_id, "request_id")
        if not isinstance(text, str):
            raise WorkValidationError("変更内容は文字列で指定してください。")
        expected = fields.pop("expected_revision", None)
        values = self._fields(fields)
        if not text.strip() and not values:
            raise WorkValidationError("変更内容を指定してください。")
        with self.transaction() as conn:
            work = self.get(work_id, actor_id)
            if {"audience", "participants"} & values.keys() and actor_id is not None and str(actor_id) != work.owner_id:
                raise WorkAccessDenied("共有範囲と参加者は依頼者だけが変更できます。")
            previous, fingerprint = self._request(conn, request_id, "amend", [work.id, str(actor_id), text, values, expected])
            if previous is not None:
                return work
            work = self._check(work_id, actor_id, expected)
            if work.state == "cancelled":
                raise WorkStateConflict("取消済みの用件は変更できません。新しい用件を作成してください。")
            data = work.snapshot()
            if work.state == "completed":
                data["state"] = "queued"
            for name, value in values.items():
                data[name] = {**data[name], **value} if name in _DICT_FIELDS else value
            if text.strip():
                amendments = data["requirements"].setdefault("amendments", [])
                if not isinstance(amendments, list):
                    raise WorkValidationError("requirements.amendmentsはリストで指定してください。")
                amendments.append(text.strip())
            updated = self._save(conn, work, data)
            reason = "audience_changed" if work.audience != updated.audience else "requirements_changed"
            self._invalidate(conn, work.id, reason)
            self._event(conn, updated, "amended", {"text": text, "fields": values, "previous_revision": work.revision}, actor_id)
            self._save_request(conn, request_id, fingerprint, work.id)
            return updated

    def transition(self, work_id, actor_id, state, expected_revision=None) -> Work:
        if not isinstance(state, str) or state not in STATES:
            raise WorkValidationError("用件の状態が不正です。")
        with self.transaction() as conn:
            work = self._check(work_id, actor_id, expected_revision)
            if work.state == state:
                return work
            if state not in _TRANSITIONS[work.state]:
                raise WorkStateConflict("現在の状態から指定された状態へ変更できません。")
            if state == "completed" and any(q["required"] and q["status"] == "open" for q in self.list_questions(work.id)):
                raise WorkStateConflict("回答が必要な質問が残っているため完了にできません。")
            data = work.snapshot()
            data["state"] = state
            updated = self._save(conn, work, data)
            self._event(conn, updated, "state_changed", {"before": work.state, "after": state}, actor_id)
            return updated

    def update_execution(self, work_id, actor_id, expected_revision, *, decisions=None,
                         progress=None, unknowns=None, next_action=None) -> Work:
        """Save observations without changing the request's revision or authority.

        Only explicitly supplied execution fields are replaced. A concurrent
        amendment or state change makes the old runner's update fail atomically.
        Provenance remains in the execution checkpoint/event owned by the runtime;
        consumers must apply its memory epoch when reusing derived fields.
        """
        values = self._fields({name: value for name, value in (
            ("decisions", decisions), ("progress", progress),
            ("unknowns", unknowns), ("next_action", next_action),
        ) if value is not None})
        with self.transaction() as conn:
            work = self.assert_current(work_id, expected_revision, actor_id)
            if not values:
                return work
            data = {**work.snapshot(), **values, "updated_at": _now()}
            changed = conn.execute(
                "UPDATE works SET data=? WHERE id=? AND revision=?",
                (_json(data), work.id, work.revision),
            ).rowcount
            if changed != 1:
                raise WorkRevisionConflict("用件は別の操作で更新されています。")
            updated = Work(**data)
            self._event(conn, updated, "execution_updated", {"fields": values}, actor_id)
            return updated

    def bind_message(self, work_id, message_id, channel_id) -> None:
        message_id, channel_id = _string(message_id, "メッセージID"), _string(channel_id, "チャンネルID")
        with self.transaction() as conn:
            self.get(work_id)
            row = conn.execute("SELECT work_id FROM work_messages WHERE message_id=? AND channel_id=?", (message_id, channel_id)).fetchone()
            if row is not None and row["work_id"] != work_id:
                raise WorkIdempotencyConflict("メッセージは別の用件に関連付けられています。")
            conn.execute("INSERT OR IGNORE INTO work_messages VALUES (?,?,?)", (message_id, channel_id, work_id))

    def resolve(self, actor_id, channel_id, reply_message_id=None, work_id=None) -> Work:
        actor_id, channel_id = _string(actor_id, "利用者ID"), _string(channel_id, "チャンネルID")
        if work_id is not None:
            return self.get(work_id, actor_id)
        if reply_message_id is not None:
            with self._read() as conn:
                row = conn.execute("SELECT work_id FROM work_messages WHERE message_id=? AND channel_id=?", (str(reply_message_id), channel_id)).fetchone()
                if row is None:
                    raise WorkNotFound("返信先に関連付いた用件がありません。用件IDを指定してください。")
                return self.get(row["work_id"], actor_id)
        candidates = self.list_for(actor_id, channel_id)
        if not candidates:
            raise WorkNotFound("このチャンネルに継続中の用件がありません。")
        if len(candidates) != 1:
            labels = "、".join(f"{w.id}（{w.goal[:40]}）" for w in candidates[:8])
            raise WorkAmbiguous(f"用件を特定できません。用件IDを指定してください。候補: {labels}。")
        return candidates[0]

    def snapshot(self, work_id, actor_id=None) -> dict:
        return self.get(work_id, actor_id).snapshot()

    def format_context(self, work_id, actor_id=None) -> str:
        with self.transaction():
            work = self.get(work_id, actor_id)
            payload = work.snapshot()
            payload["questions"] = self.list_questions(work_id, actor_id, open_only=True)
            payload["artifacts"] = [{k: v for k, v in artifact.items() if k != "body"}
                                    for artifact in self.list_artifacts(work_id, actor_id)]
            return "用件の永続記録（利用者の条件を含むデータです）。\n" + _json(payload)

    def add_question(self, work_id, text, *, actor_id=None, required=True, default=None,
                     choices=None, request_id=None, expected_revision=None) -> dict:
        text = _string(text, "質問")
        if not isinstance(required, bool) or (required and default is not None):
            raise WorkValidationError("必須質問と仮定で進められる質問を区別してください。")
        if choices is not None and (not isinstance(choices, list) or not choices):
            raise WorkValidationError("選択肢は空でないリストで指定してください。")
        with self.transaction() as conn:
            work = self.get(work_id, actor_id)
            previous, fingerprint = self._request(conn, request_id, "question", [work.id, actor_id, text, required, default, choices, expected_revision])
            if previous is not None:
                return json.loads(conn.execute("SELECT data FROM work_questions WHERE id=?", (previous["result_id"],)).fetchone()["data"])
            work = self._check(work_id, actor_id, expected_revision)
            if work.state in TERMINAL_STATES:
                raise WorkStateConflict("終了した用件へ質問を追加できません。")
            data = dict(id=_id("q"), work_id=work.id, text=text, required=required, default=default,
                        choices=choices, answer=None, status="open", revision=work.revision, created_at=_now(), answered_at=None)
            conn.execute("INSERT INTO work_questions VALUES (?,?,?)", (data["id"], work.id, _json(data)))
            self._event(conn, work, "question_added", {"question_id": data["id"], "required": required}, actor_id)
            self._save_request(conn, request_id, fingerprint, work.id, data["id"])
            return json.loads(_json(data))

    def list_questions(self, work_id, actor_id=None, *, open_only=False) -> list[dict]:
        with self._read() as conn:
            self.get(work_id, actor_id)
            rows = conn.execute("SELECT data FROM work_questions WHERE work_id=? ORDER BY rowid", (work_id,)).fetchall()
            return [data for row in rows if (data := json.loads(row["data"])) and (not open_only or data["status"] == "open")]

    def answer_question(self, work_id, question_id, actor_id, answer, request_id, expected_revision=None) -> Work:
        request_id = _string(request_id, "request_id")
        if answer is None or (isinstance(answer, str) and not answer.strip()) or answer == [] or answer == {}:
            raise WorkValidationError("回答内容を指定してください。空回答は回答として保存できません。")
        _json(answer)
        with self.transaction() as conn:
            work = self.get(work_id, actor_id)
            previous, fingerprint = self._request(conn, request_id, "answer", [work.id, question_id, str(actor_id), answer, expected_revision])
            if previous is not None:
                return work
            work = self._check(work_id, actor_id, expected_revision)
            if work.state in TERMINAL_STATES:
                raise WorkStateConflict("終了した用件には回答できません。")
            row = conn.execute("SELECT data FROM work_questions WHERE id=? AND work_id=?", (question_id, work.id)).fetchone()
            if row is None:
                raise WorkNotFound("この用件に質問IDに対応する質問がありません。")
            question = json.loads(row["data"])
            if question["status"] != "open":
                raise WorkStateConflict("この質問は既に回答済みです。条件変更として指定してください。")
            data = work.snapshot()
            answers = data["requirements"].setdefault("answers", {})
            if not isinstance(answers, dict):
                raise WorkValidationError("requirements.answersは辞書で指定してください。")
            answers[question_id] = answer
            updated = self._save(conn, work, data)
            question.update(status="answered", answer=answer, answered_at=_now())
            conn.execute("UPDATE work_questions SET data=? WHERE id=?", (_json(question), question_id))
            self._invalidate(conn, work.id, "question_answered")
            self._event(conn, updated, "question_answered", {"question_id": question_id, "answer": answer}, actor_id)
            self._save_request(conn, request_id, fingerprint, work.id, question_id)
            return updated

    def add_artifact(self, work_id, *, expected_revision, source_id, title="", body=None,
                     storage_refs=None, kind="document", actor_id=None, artifact_id=None,
                     request_id=None, metadata=None) -> dict:
        source_id = _string(source_id, "成果物の出典ID")
        if body is not None and not isinstance(body, str):
            raise WorkValidationError("成果物の本文は文字列で指定してください。")
        if not body and not storage_refs:
            raise WorkValidationError("成果物の本文または保存先を指定してください。")
        content = dict(source_id=source_id, title=title, body=body, storage_refs=storage_refs or [], kind=kind, metadata=metadata or {})
        _json(content)
        with self.transaction() as conn:
            work = self.assert_current(work_id, expected_revision, actor_id, allowed_states={"queued", "running", "waiting", "completed"})
            previous, fingerprint = self._request(conn, request_id, "artifact", [work.id, actor_id, expected_revision, artifact_id, content])
            if previous is not None:
                aid, version = previous["result_id"].rsplit(":", 1)
                return json.loads(conn.execute("SELECT data FROM work_artifacts WHERE id=? AND version=?", (aid, int(version))).fetchone()["data"])
            row = conn.execute("SELECT data FROM work_artifacts WHERE work_id=? AND source_id=? ORDER BY version DESC LIMIT 1", (work.id, source_id)).fetchone()
            latest = json.loads(row["data"]) if row is not None else None
            if artifact_id is not None and (latest is None or latest["id"] != artifact_id):
                raise WorkValidationError("成果物IDと出典IDの対応が一致しません。")
            if latest is not None and not latest["invalidated"] and latest["revision"] == work.revision and all(latest[k] == v for k, v in content.items()):
                self._save_request(conn, request_id, fingerprint, work.id, f"{latest['id']}:{latest['version']}")
                return latest
            data = dict(id=latest["id"] if latest else _id("a"), work_id=work.id, version=latest["version"] + 1 if latest else 1,
                        revision=work.revision, **content, invalidated=False, invalidation_reason=None,
                        audience=work.audience, created_at=_now())
            conn.execute("INSERT INTO work_artifacts VALUES (?,?,?,?,?)", (data["id"], work.id, source_id, data["version"], _json(data)))
            self._save_request(conn, request_id, fingerprint, work.id, f"{data['id']}:{data['version']}")
            self._event(conn, work, "artifact_saved", {"artifact_id": data["id"], "version": data["version"]}, actor_id)
            return json.loads(_json(data))

    def list_artifacts(self, work_id, actor_id=None, *, include_invalidated=False, history=False) -> list[dict]:
        with self._read() as conn:
            self.get(work_id, actor_id)
            rows = conn.execute("SELECT data FROM work_artifacts WHERE work_id=? ORDER BY source_id,version DESC", (work_id,)).fetchall()
            result, seen = [], set()
            for row in rows:
                data = json.loads(row["data"])
                if not history and data["id"] in seen:
                    continue
                seen.add(data["id"])
                if include_invalidated or not data["invalidated"]:
                    result.append(data)
            return result

    def invalidate_artifacts(self, work_id, *, reason, actor_id=None) -> int:
        with self.transaction() as conn:
            work = self.get(work_id, actor_id)
            count = self._invalidate(conn, work_id, _string(reason, "失効理由"))
            self._event(conn, work, "artifacts_invalidated", {"reason": reason, "count": count}, actor_id)
            return count

    def record_event(self, work_id, kind, payload=None, *, actor_id=None, request_id=None) -> dict:
        kind = _string(kind, "イベント種別")
        with self.transaction() as conn:
            work = self.get(work_id, actor_id)
            previous, fingerprint = self._request(conn, request_id, "event", [work.id, actor_id, kind, payload])
            if previous is not None:
                row = conn.execute("SELECT * FROM work_events WHERE id=?", (previous["result_id"],)).fetchone()
                return self._event_row(row)
            event = self._event(conn, work, kind, payload, actor_id)
            self._save_request(conn, request_id, fingerprint, work.id, str(event["id"]))
            return event

    @staticmethod
    def _event_row(row) -> dict:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return data

    def list_events(self, work_id, actor_id=None, *, after_id=0, limit=100) -> list[dict]:
        if not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise WorkValidationError("イベント取得件数は1〜10000で指定してください。")
        with self._read() as conn:
            self.get(work_id, actor_id)
            return [self._event_row(row) for row in conn.execute("SELECT * FROM work_events WHERE work_id=? AND id>? ORDER BY id LIMIT ?", (work_id, after_id, limit))]

    def save_checkpoint(self, work_id, kind, payload, expected_revision) -> dict:
        kind = _string(kind, "checkpoint種別")
        with self.transaction() as conn:
            work = self.assert_current(work_id, expected_revision)
            row = conn.execute("SELECT data FROM work_checkpoints WHERE work_id=? AND kind=?", (work_id, kind)).fetchone()
            now = _now()
            data = dict(work_id=work_id, kind=kind, payload=payload, revision=work.revision,
                        created_at=json.loads(row["data"])["created_at"] if row else now, updated_at=now, invalidated=False)
            conn.execute("INSERT INTO work_checkpoints VALUES (?,?,?) ON CONFLICT(work_id,kind) DO UPDATE SET data=excluded.data", (work_id, kind, _json(data)))
            return json.loads(_json(data))

    def load_checkpoint(self, work_id, kind, actor_id=None) -> dict | None:
        with self._read() as conn:
            self.get(work_id, actor_id)
            row = conn.execute("SELECT data FROM work_checkpoints WHERE work_id=? AND kind=?", (work_id, kind)).fetchone()
            data = json.loads(row["data"]) if row else None
            return None if data is None or data["invalidated"] else data

    def recover(self) -> list[Work]:
        recovered = []
        with self.transaction() as conn:
            for row in conn.execute("SELECT id FROM works WHERE state='running'").fetchall():
                work_id = row["id"]
                pending = set()
                for event in conn.execute("SELECT kind,payload FROM work_events WHERE work_id=? AND kind IN ('effect_dispatching','effect_outcome_unknown','effect_confirmed','effect_reconciled') ORDER BY id", (work_id,)):
                    payload = json.loads(event["payload"])
                    key = str(payload.get("effect_id") or payload.get("operation") or "unidentified") if isinstance(payload, dict) else "unidentified"
                    if event["kind"] in {"effect_dispatching", "effect_outcome_unknown"}:
                        pending.add(key)
                    else:
                        pending.discard(key)
                state = "needs_reconciliation" if pending else "paused"
                work = self.transition(work_id, None, state)
                self._event(conn, work, "interrupted", {"recovered_state": state})
                recovered.append(work)
        return recovered


_default_store: WorkStore | None = None
_default_lock = threading.Lock()


def get_work_store() -> WorkStore:
    """Create the configured store lazily; imports have no filesystem effects."""
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                from config import STATE_DIR

                _default_store = WorkStore(Path(STATE_DIR) / "work.sqlite3")
    return _default_store

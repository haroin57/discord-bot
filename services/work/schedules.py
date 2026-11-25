"""Durable opt-in reminders and condition watches using mediated effects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import inspect
import json
from pathlib import Path
import time
from typing import Any, Callable
import uuid

from services.work.effects import EffectExecutor, ScopeError, _json


@dataclass(frozen=True)
class Observation:
    value: Any
    content: str
    matched: bool = True


@dataclass(frozen=True)
class Schedule:
    id: str
    owner_id: str
    recipient: str
    kind: str
    observer: str | None
    target: str
    criteria: dict
    content: str
    work_id: str | None
    revision: int | None
    grant_id: str
    status: str
    next_due: float
    expires_at: float
    interval_seconds: float
    cooldown_seconds: float
    diff_only: bool
    stop_rule: str
    last_fingerprint: str | None
    last_notified_at: float | None
    notifications: int
    pending: dict | None
    error: str | None
    lease_token: str | None
    lease_until: float | None
    created_at: float
    updated_at: float


def evaluate_criteria(value: Any, criteria: dict) -> bool:
    """Evaluate data-only predicates; no expressions, URLs, or executable scripts."""
    if not criteria:
        return True
    if "all" in criteria:
        return all(evaluate_criteria(value, part) for part in criteria["all"])
    if "any" in criteria:
        return any(evaluate_criteria(value, part) for part in criteria["any"])
    if "field" not in criteria:
        return isinstance(value, dict) and all(value.get(key) == expected for key, expected in criteria.items())
    actual = value
    for key in str(criteria["field"]).split("."):
        if not isinstance(actual, dict) or key not in actual:
            return criteria.get("op") == "missing"
        actual = actual[key]
    expected, op = criteria.get("value"), criteria.get("op", "eq")
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "contains":
            return expected in actual
        if op == "exists":
            return True
        if op == "missing":
            return False
    except (TypeError, ValueError):
        return False
    raise ValueError("unsupported condition operator")


class ScheduleService:
    def __init__(self, database_path: str | Path, executor: EffectExecutor, *, clock: Callable[[], float] = time.time,
                 publication_policy: Callable[[Schedule], bool] | None = None):
        if str(database_path) != executor.path:
            raise ValueError("schedules and effects must share one database for atomic cancellation gates")
        self.path = str(database_path)
        self.executor = executor
        self.clock = clock
        self.observers: dict[str, Callable] = {}
        self.authorizers: dict[str, Callable] = {}
        self.publication_policy: Callable[[Schedule], bool] | None = None
        self.set_publication_policy(publication_policy)
        executor.schedule_gate = self._scope_gate
        with executor.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS yui_schedules (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, recipient TEXT NOT NULL,
                    kind TEXT NOT NULL, observer TEXT, target TEXT NOT NULL,
                    criteria_json TEXT NOT NULL, content TEXT NOT NULL,
                    work_id TEXT, revision INTEGER, grant_id TEXT NOT NULL,
                    status TEXT NOT NULL, next_due REAL NOT NULL, expires_at REAL NOT NULL,
                    interval_seconds REAL NOT NULL, cooldown_seconds REAL NOT NULL,
                    diff_only INTEGER NOT NULL, stop_rule TEXT NOT NULL,
                    last_fingerprint TEXT, last_notified_at REAL, notifications INTEGER NOT NULL DEFAULT 0,
                    pending_json TEXT, error TEXT, lease_token TEXT, lease_until REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS yui_schedule_due ON yui_schedules(status, next_due);
                CREATE TABLE IF NOT EXISTS yui_schedule_requests (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    schedule_id TEXT NOT NULL REFERENCES yui_schedules(id)
                );
            """)

    def register_observer(self, name: str, observer: Callable) -> None:
        if not name or not callable(observer):
            raise ValueError("named observer is required")
        self.observers[name] = observer

    def register_authorizer(self, name: str, authorize: Callable) -> None:
        """Recheck owner/target/recipient atomically at notification submission."""
        if inspect.iscoroutinefunction(authorize):
            raise ValueError("submission authorizer must be synchronous")
        self.authorizers[name] = authorize

    def set_publication_policy(self, policy: Callable[[Schedule], bool] | None) -> None:
        """Install a synchronous final ledger/audience check for every notification.

        The callback sees the saved Schedule and its exact pending payload while
        the effect start transaction is held. It must not perform network I/O or
        write to this SQLite database through a different connection.
        """
        if policy is not None and (not callable(policy) or inspect.iscoroutinefunction(policy)):
            raise ValueError("publication policy must be a synchronous callback")
        self.publication_policy = policy

    @staticmethod
    def _allowed(callback, *args) -> bool:
        try:
            result = callback(*args)
        except Exception:
            return False
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            return False
        return bool(result)

    def _scope_gate(self, row: dict) -> bool:
        if self.publication_policy is not None and not self._allowed(self.publication_policy, self._schedule(row)):
            return False
        authorize = self.authorizers.get(row["observer"])
        if authorize is None:
            # Public non-work observation sources may enforce their own scope.
            # Private work watchers must always have an independent final gate.
            return row["observer"] != "work"
        return self._allowed(authorize, row["owner_id"], row["target"], row["recipient"])

    @staticmethod
    def _schedule(row) -> Schedule:
        data = dict(row)
        data["criteria"] = json.loads(data.pop("criteria_json"))
        pending = data.pop("pending_json")
        data["pending"] = json.loads(pending) if pending else None
        data["diff_only"] = bool(data["diff_only"])
        return Schedule(**data)

    def get(self, schedule_id: str, owner_id: str | None = None) -> Schedule:
        with self.executor.transaction() as db:
            row = db.execute("SELECT * FROM yui_schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None or (owner_id is not None and row["owner_id"] != str(owner_id)):
                raise ScopeError("schedule not found for owner")
            return self._schedule(row)

    def list(self, owner_id: str, include_inactive: bool = False) -> list[Schedule]:
        with self.executor.transaction() as db:
            return [self._schedule(row) for row in db.execute(
                "SELECT * FROM yui_schedules WHERE owner_id=?" +
                ("" if include_inactive else " AND status='active'") + " ORDER BY created_at DESC",
                (str(owner_id),),
            )]

    def _replayed(self, db, request_id, fingerprint):
        if request_id is None:
            return None
        request = db.execute("SELECT * FROM yui_schedule_requests WHERE request_id=?", (request_id,)).fetchone()
        if request is None:
            return None
        if request["fingerprint"] != fingerprint:
            raise ScopeError("source request ID was reused with different owner or notification conditions")
        row = db.execute("SELECT * FROM yui_schedules WHERE id=?", (request["schedule_id"],)).fetchone()
        if row is None:
            raise ScopeError("saved notification request has no schedule; reconciliation is required")
        return self._schedule(row)

    def _add(self, *, owner_id, recipient, kind, observer, target, criteria, content,
             run_at, expires_at, interval_seconds, cooldown_seconds, diff_only, stop_rule,
             work_id, revision, request_id=None):
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            raise ValueError("source request ID must be a non-empty string")
        fingerprint = hashlib.sha256(_json({
            "owner_id": str(owner_id), "recipient": str(recipient), "kind": kind,
            "observer": observer, "target": str(target), "criteria": criteria, "content": content,
            # The initial watch check time is generated at registration, not
            # supplied by the requester, and must not defeat transport replay.
            "run_at": run_at if kind == "reminder" else None, "expires_at": expires_at,
            "interval_seconds": interval_seconds, "cooldown_seconds": cooldown_seconds,
            "diff_only": bool(diff_only), "stop_rule": stop_rule,
            "work_id": work_id, "revision": revision,
        }).encode()).hexdigest()
        with self.executor.transaction() as db:
            existing = self._replayed(db, request_id, fingerprint)
            if existing is not None:
                return existing
        if kind == "reminder" and run_at <= self.clock():
            raise ValueError("reminder must be scheduled in the future")
        if kind == "watch" and observer not in self.observers:
            raise ValueError("requested observation source is unavailable")
        if expires_at <= self.clock() or expires_at <= run_at:
            raise ValueError("expiry must be later than the scheduled execution")
        if interval_seconds < 1 or cooldown_seconds < 0:
            raise ValueError("invalid interval or cooldown")
        if stop_rule not in {"expiry", "first_match"}:
            raise ValueError("stop rule must be expiry or first_match")
        if len(content) > 3500:
            raise ValueError("notification content exceeds 3500 characters")
        # A grant alone is inert; persisting it before the schedule can leave only
        # an unused grant on a crash, never an acknowledged but missing schedule.
        constraints = {"channel_id": str(recipient)}
        if kind == "reminder":
            constraints["content"] = content
        grant_id = self.executor.authorize(
            str(owner_id), "discord", "notify", str(recipient),
            payload_constraints=constraints, allowed_payload_keys=["channel_id", "content"],
            work_id=work_id, revision=revision, expires_at=expires_at,
            max_executions=1 if kind == "reminder" or stop_rule == "first_match" else None,
        )
        sid, now = "watch_" + uuid.uuid4().hex[:16], self.clock()
        with self.executor.transaction() as db:
            # Another process may have committed this request while its grant
            # was being created. Reuse its schedule and leave our grant inert.
            existing = self._replayed(db, request_id, fingerprint)
            if existing is not None:
                return existing
            db.execute(
                """INSERT INTO yui_schedules(id,owner_id,recipient,kind,observer,target,criteria_json,
                    content,work_id,revision,grant_id,status,next_due,expires_at,interval_seconds,
                    cooldown_seconds,diff_only,stop_rule,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?)""",
                (sid, str(owner_id), str(recipient), kind, observer, str(target), _json(criteria),
                 content, work_id, revision, grant_id, run_at, expires_at, interval_seconds,
                 cooldown_seconds, int(diff_only), stop_rule, now, now),
            )
            if request_id is not None:
                db.execute("INSERT INTO yui_schedule_requests VALUES(?,?,?)", (request_id, fingerprint, sid))
        return self.get(sid)

    def add_reminder(self, owner_id: str, recipient: str, content: str, *, run_at: float,
                     expires_at: float | None = None, work_id: str | None = None,
                     revision: int | None = None, request_id: str | None = None) -> Schedule:
        if not content.strip():
            raise ValueError("reminder content is required")
        return self._add(
            owner_id=owner_id, recipient=recipient, kind="reminder", observer=None,
            target=str(recipient), criteria={}, content=content, run_at=run_at,
            expires_at=expires_at if expires_at is not None else run_at + 86400,
            interval_seconds=60, cooldown_seconds=0, diff_only=True, stop_rule="first_match",
            work_id=work_id, revision=revision, request_id=request_id,
        )

    def add_watch(self, owner_id: str, recipient: str, *, observer: str, target: str,
                  criteria: dict, expires_at: float, interval_seconds: float = 300,
                  cooldown_seconds: float = 300, diff_only: bool = True,
                  stop_rule: str = "expiry", work_id: str | None = None,
                  revision: int | None = None, request_id: str | None = None) -> Schedule:
        if not target:
            raise ValueError("observation target is required")
        return self._add(
            owner_id=owner_id, recipient=recipient, kind="watch", observer=observer,
            target=target, criteria=criteria, content="", run_at=self.clock(),
            expires_at=expires_at, interval_seconds=interval_seconds,
            cooldown_seconds=cooldown_seconds, diff_only=diff_only, stop_rule=stop_rule,
            work_id=work_id, revision=revision, request_id=request_id,
        )

    def cancel(self, schedule_id: str, owner_id: str) -> Schedule:
        with self.executor.transaction() as db:
            row = db.execute("SELECT * FROM yui_schedules WHERE id=? AND owner_id=?",
                             (schedule_id, str(owner_id))).fetchone()
            if row is None:
                raise ScopeError("schedule not found for owner")
            db.execute("UPDATE yui_schedules SET status='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                       (self.clock(), schedule_id))
            db.execute("UPDATE yui_effect_grants SET revoked=1 WHERE id=?", (row["grant_id"],))
            db.execute("UPDATE yui_effects SET state='cancelled',updated_at=? WHERE schedule_id=? AND state='accepted'",
                       (self.clock(), schedule_id))
        return self.get(schedule_id)

    def _claim(self, limit=20):
        now = self.clock()
        with self.executor.transaction() as db:
            db.execute("UPDATE yui_schedules SET status='expired',updated_at=? WHERE status='active' AND expires_at<=?",
                       (now, now))
            rows = db.execute(
                """SELECT * FROM yui_schedules WHERE status='active' AND next_due<=?
                    AND (lease_until IS NULL OR lease_until<=?) ORDER BY next_due LIMIT ?""",
                (now, now, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                token = uuid.uuid4().hex
                db.execute("UPDATE yui_schedules SET lease_token=?,lease_until=? WHERE id=?",
                           (token, now + 120, row["id"]))
                data = dict(row)
                data.update(lease_token=token, lease_until=now + 120)
                claimed.append(self._schedule(data))
            return claimed

    def _finish_check(self, schedule, *, pending=None, fingerprint=None, notified=False,
                      completed=False, error=None):
        now = self.clock()
        with self.executor.transaction() as db:
            db.execute(
                """UPDATE yui_schedules SET pending_json=?,
                    last_fingerprint=COALESCE(?,last_fingerprint),
                    last_notified_at=CASE WHEN ? THEN ? ELSE last_notified_at END,
                    notifications=notifications+?, status=CASE WHEN ? THEN 'completed' ELSE status END,
                    next_due=?, error=?,lease_token=NULL,lease_until=NULL,updated_at=?
                    WHERE id=? AND lease_token=? AND status='active'""",
                (_json(pending) if pending else None, fingerprint, int(notified), now,
                 int(notified), int(completed), now + schedule.interval_seconds, error, now,
                 schedule.id, schedule.lease_token),
            )

    async def _check(self, schedule):
        pending = schedule.pending
        try:
            if pending is None:
                if schedule.kind == "reminder":
                    observation = Observation(schedule.content, schedule.content)
                else:
                    observer = self.observers.get(schedule.observer)
                    if observer is None:
                        raise ValueError("observation source is unavailable")
                    # The third argument carries owner/audience for authorization.
                    params = inspect.signature(observer).parameters
                    result = observer(schedule.target, schedule.criteria, schedule) if len(params) >= 3 else observer(schedule.target, schedule.criteria)
                    observation = await asyncio.wait_for(result, 45) if inspect.isawaitable(result) else result
                    if not isinstance(observation, Observation):
                        raise TypeError("observer must return Observation")
                if not observation.matched or not evaluate_criteria(observation.value, schedule.criteria):
                    self._finish_check(schedule)
                    return
                fingerprint = hashlib.sha256(_json(observation.value).encode()).hexdigest()
                if schedule.diff_only and fingerprint == schedule.last_fingerprint:
                    self._finish_check(schedule)
                    return
                if schedule.last_notified_at is not None and self.clock() < schedule.last_notified_at + schedule.cooldown_seconds:
                    self._finish_check(schedule)
                    return
                pending = {
                    "key": f"{schedule.id}:{schedule.next_due}",
                    "fingerprint": fingerprint,
                    "payload": {"channel_id": schedule.recipient, "content": observation.content[:3500]},
                }
                # Save the exact observation/key before accepting its effect.
                # Recovery must not replace it with a new observation and resend.
                with self.executor.transaction() as db:
                    saved = db.execute(
                        "UPDATE yui_schedules SET pending_json=? WHERE id=? AND lease_token=? AND status='active' AND expires_at>?",
                        (_json(pending), schedule.id, schedule.lease_token, self.clock()),
                    ).rowcount
                    if not saved:
                        return
            receipt = self.executor.accept(
                schedule.grant_id, schedule.owner_id, pending["payload"],
                idempotency_key=pending["key"], work_id=schedule.work_id,
                revision=schedule.revision, schedule_id=schedule.id,
            )
            receipt = await self.executor.execute(receipt.id)
            pending["effect_id"] = receipt.id
            if receipt.state == "confirmed":
                self._finish_check(
                    schedule, fingerprint=pending["fingerprint"], notified=True,
                    completed=schedule.stop_rule == "first_match" or schedule.kind == "reminder",
                )
            elif receipt.state in {"rejected", "cancelled"}:
                self._finish_check(schedule, pending=pending, error=receipt.error or receipt.state)
            else:
                self._finish_check(schedule, pending=pending, error="操作結果を確認中です。再送信は行いません。")
        except asyncio.CancelledError:
            # Keep pending data for restart and release only our lease.
            self._finish_check(schedule, pending=pending, error="処理が中断しました。")
            raise
        except Exception as exc:
            self._finish_check(schedule, pending=pending, error=f"{type(exc).__name__}: {exc}"[:500])

    async def tick(self) -> int:
        claimed = self._claim()
        # Sequential checks keep independent adapters below their configured cap;
        # concurrent scheduler instances are protected by leases and effect claims.
        for schedule in claimed:
            await self._check(schedule)
        return len(claimed)

    async def run(self, stop_event: asyncio.Event, poll_seconds: float = 15):
        while not stop_event.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging
                logging.getLogger(__name__).exception("durable schedule tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass

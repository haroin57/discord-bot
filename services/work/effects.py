"""Durable, explicitly authorized external effects with conservative recovery.

This executor mediates adapters registered with it. It does not claim to sandbox
an unrelated process that independently holds the same external credentials.
The submission transaction is the operation's start boundary: work revisions and
cancellation on the same SQLite database serialize before or after that point.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Protocol
import uuid


class ScopeError(ValueError):
    """An operation exceeds its stored authorization or current work revision."""


@dataclass(frozen=True)
class AdapterCapabilities:
    actions: tuple[str, ...]
    idempotency: bool
    reconciliation: bool
    cancellation: bool
    completion_semantics: str
    target_field: str = "target"


@dataclass(frozen=True)
class AdapterResult:
    state: str = "confirmed"
    remote_id: str | None = None
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class EffectReceipt:
    id: str
    grant_id: str
    owner_id: str
    work_id: str | None
    revision: int | None
    schedule_id: str | None
    adapter: str
    action: str
    target: str
    payload: dict[str, Any]
    idempotency_key: str
    state: str
    remote_id: str | None
    result: dict[str, Any]
    error: str | None
    created_at: float
    updated_at: float
    lease_until: float | None


class EffectAdapter(Protocol):
    capabilities: AdapterCapabilities

    async def submit(self, payload: dict, *, idempotency_key: str) -> AdapterResult: ...

    async def reconcile(self, receipt: EffectReceipt) -> AdapterResult | None: ...


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EffectExecutor:
    def __init__(
        self,
        database_path: str | Path,
        *,
        work_gate: Callable[[str, int | None, str], bool] | None = None,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 120,
        submit_timeout: float = 30,
    ):
        self.path = str(database_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.work_gate = work_gate
        self.clock = clock
        self.lease_seconds = max(lease_seconds, submit_timeout + 5)
        self.submit_timeout = submit_timeout
        self.adapters: dict[str, EffectAdapter] = {}
        self.schedule_gate: Callable[[dict], bool] | None = None
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS yui_effect_grants (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, adapter TEXT NOT NULL,
                    action TEXT NOT NULL, target TEXT NOT NULL, constraints_json TEXT NOT NULL,
                    allowed_keys_json TEXT, work_id TEXT, revision INTEGER,
                    expires_at REAL, max_executions INTEGER, revoked INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yui_effects (
                    id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    work_id TEXT, revision INTEGER, schedule_id TEXT,
                    adapter TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
                    payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    state TEXT NOT NULL, remote_id TEXT, result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    lease_until REAL, UNIQUE(grant_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS yui_effect_owner ON yui_effects(owner_id, created_at);
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register_adapter(self, name: str, adapter: EffectAdapter) -> None:
        caps = adapter.capabilities
        if not caps.actions or not caps.completion_semantics:
            raise ValueError("adapter must declare actions and completion semantics")
        if caps.reconciliation and not callable(getattr(adapter, "reconcile", None)):
            raise ValueError("adapter does not implement its reconciliation capability")
        if caps.cancellation and not callable(getattr(adapter, "cancel", None)):
            raise ValueError("adapter does not implement its cancellation capability")
        self.adapters[name] = adapter

    def authorize(
        self,
        owner_id: str,
        adapter: str,
        action: str,
        target: str,
        *,
        payload_constraints: dict | None = None,
        allowed_payload_keys: list[str] | None = None,
        work_id: str | None = None,
        revision: int | None = None,
        expires_at: float | None = None,
        max_executions: int | None = None,
    ) -> str:
        """Persist an actual user's authorization; this is not a model tool."""
        if adapter not in self.adapters or action not in self.adapters[adapter].capabilities.actions:
            raise ScopeError("adapter/action is unavailable")
        if not str(owner_id) or not str(target):
            raise ScopeError("owner and exact target are required")
        if expires_at is not None and expires_at <= self.clock():
            raise ScopeError("authorization already expired")
        if max_executions is not None and max_executions < 1:
            raise ScopeError("execution limit must be positive")
        constraints = dict(payload_constraints or {})
        target_field = self.adapters[adapter].capabilities.target_field
        if target_field in constraints and constraints[target_field] != str(target):
            raise ScopeError("payload target conflicts with authorized target")
        constraints[target_field] = str(target)
        gid = "grant_" + uuid.uuid4().hex
        with self.transaction() as db:
            db.execute(
                "INSERT INTO yui_effect_grants VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (gid, str(owner_id), adapter, action, str(target), _json(constraints),
                 _json(allowed_payload_keys) if allowed_payload_keys is not None else None,
                 work_id, revision, expires_at, max_executions, self.clock()),
            )
        return gid

    def revoke(self, grant_id: str, owner_id: str) -> None:
        with self.transaction() as db:
            if not db.execute(
                "UPDATE yui_effect_grants SET revoked=1 WHERE id=? AND owner_id=?",
                (grant_id, str(owner_id)),
            ).rowcount:
                raise ScopeError("authorization not found for owner")
            db.execute(
                "UPDATE yui_effects SET state='cancelled', updated_at=? WHERE grant_id=? AND state='accepted'",
                (self.clock(), grant_id),
            )

    def _validate_grant(self, db, grant, owner_id, payload, work_id, revision):
        if grant is None or grant["owner_id"] != str(owner_id) or grant["revoked"]:
            raise ScopeError("authorization is missing, revoked, or belongs to another owner")
        if grant["expires_at"] is not None and grant["expires_at"] <= self.clock():
            raise ScopeError("authorization expired")
        if grant["work_id"] != work_id or grant["revision"] != revision:
            raise ScopeError("authorization does not cover this work revision")
        constraints = json.loads(grant["constraints_json"])
        if any(key not in payload or payload[key] != value for key, value in constraints.items()):
            raise ScopeError("payload exceeds exact authorized constraints")
        if grant["allowed_keys_json"] is not None:
            if set(payload) - set(json.loads(grant["allowed_keys_json"])):
                raise ScopeError("payload includes unauthorized fields")

    def accept(
        self,
        grant_id: str,
        owner_id: str,
        payload: dict,
        *,
        idempotency_key: str,
        work_id: str | None = None,
        revision: int | None = None,
        schedule_id: str | None = None,
    ) -> EffectReceipt:
        if not idempotency_key:
            raise ValueError("stable idempotency key is required")
        with self.transaction() as db:
            grant = db.execute("SELECT * FROM yui_effect_grants WHERE id=?", (grant_id,)).fetchone()
            self._validate_grant(db, grant, owner_id, payload, work_id, revision)
            old = db.execute(
                "SELECT * FROM yui_effects WHERE grant_id=? AND idempotency_key=?",
                (grant_id, idempotency_key),
            ).fetchone()
            if old:
                if old["payload_json"] != _json(payload) or old["schedule_id"] != schedule_id:
                    raise ScopeError("idempotency key reused for different operation")
                return self._receipt(old)
            if grant["max_executions"] is not None:
                count = db.execute(
                    "SELECT count(*) FROM yui_effects WHERE grant_id=? AND state!='cancelled'",
                    (grant_id,),
                ).fetchone()[0]
                if count >= grant["max_executions"]:
                    raise ScopeError("authorization execution limit reached")
            eid, now = "effect_" + uuid.uuid4().hex, self.clock()
            db.execute(
                """INSERT INTO yui_effects(id,grant_id,owner_id,work_id,revision,schedule_id,
                    adapter,action,target,payload_json,idempotency_key,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?)""",
                (eid, grant_id, str(owner_id), work_id, revision, schedule_id, grant["adapter"],
                 grant["action"], grant["target"], _json(payload), idempotency_key, now, now),
            )
            return self._receipt(db.execute("SELECT * FROM yui_effects WHERE id=?", (eid,)).fetchone())

    @staticmethod
    def _receipt(row) -> EffectReceipt:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        data["result"] = json.loads(data.pop("result_json"))
        return EffectReceipt(**data)

    def get(self, effect_id: str) -> EffectReceipt:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM yui_effects WHERE id=?", (effect_id,)).fetchone()
            if row is None:
                raise KeyError(effect_id)
            return self._receipt(row)

    def list_effects(self, owner_id: str, work_id: str | None = None) -> list[EffectReceipt]:
        with self.transaction() as db:
            query = "SELECT * FROM yui_effects WHERE owner_id=?"
            args: list = [str(owner_id)]
            if work_id is not None:
                query += " AND work_id=?"
                args.append(work_id)
            return [self._receipt(r) for r in db.execute(query + " ORDER BY created_at DESC", args)]

    def recover(self) -> list[EffectReceipt]:
        """Mark orphaned submissions unknown and report unresolved work effects.

        This performs no external operation and never retries submission. A live
        lease is retained when another executor may still own it.
        """
        with self.transaction() as db:
            db.execute(
                """UPDATE yui_effects SET state='needs_reconciliation',lease_until=NULL,
                    error='process stopped before remote outcome confirmation',updated_at=?
                    WHERE state='submitted' AND (lease_until IS NULL OR lease_until<=?)""",
                (self.clock(), self.clock()),
            )
            return [self._receipt(row) for row in db.execute(
                "SELECT * FROM yui_effects WHERE state IN ('submitted','remote_accepted','needs_reconciliation') ORDER BY created_at"
            )]

    def pending_work_ids(self) -> set[str]:
        return {receipt.work_id for receipt in self.recover() if receipt.work_id is not None}

    def _record(self, effect_id, *, result=None, error=None, state=None):
        if result is not None:
            if result.state not in {"remote_accepted", "confirmed", "needs_reconciliation"}:
                raise ValueError("invalid adapter outcome")
            state = result.state
        with self.transaction() as db:
            db.execute(
                """UPDATE yui_effects SET state=?, remote_id=COALESCE(?,remote_id),
                    result_json=CASE WHEN ? IS NULL THEN result_json ELSE ? END,
                    error=?, updated_at=?, lease_until=NULL WHERE id=?""",
                (state, result.remote_id if result else None,
                 _json(result.result or {}) if result else None,
                 _json(result.result or {}) if result else None, error, self.clock(), effect_id),
            )
        return self.get(effect_id)

    async def execute(self, effect_id: str) -> EffectReceipt:
        """Submit once; all uncertain or interrupted attempts require reconciliation."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM yui_effects WHERE id=?", (effect_id,)).fetchone()
            if row is None:
                raise KeyError(effect_id)
            receipt = self._receipt(row)
            if receipt.state in {"confirmed", "cancelled", "rejected"}:
                return receipt
            if receipt.lease_until is not None and receipt.lease_until > self.clock():
                return receipt
            adapter = self.adapters.get(receipt.adapter)
            if adapter is None:
                raise ScopeError("adapter is unavailable")
            first_submit = receipt.state == "accepted"
            if first_submit:
                grant = db.execute("SELECT * FROM yui_effect_grants WHERE id=?", (receipt.grant_id,)).fetchone()
                try:
                    self._validate_grant(db, grant, receipt.owner_id, receipt.payload, receipt.work_id, receipt.revision)
                    if receipt.work_id is not None:
                        if self.work_gate is None or not self.work_gate(receipt.work_id, receipt.revision, receipt.owner_id):
                            raise ScopeError("work is stale, paused, cancelled, or inaccessible")
                    if receipt.schedule_id is not None:
                        schedule = db.execute(
                            "SELECT * FROM yui_schedules WHERE id=?", (receipt.schedule_id,),
                        ).fetchone()
                        if schedule is None or schedule["status"] != "active" or schedule["expires_at"] <= self.clock():
                            raise ScopeError("schedule is inactive or expired")
                        if self.schedule_gate is not None and not self.schedule_gate(dict(schedule)):
                            raise ScopeError("schedule target/audience authorization is no longer valid")
                except ScopeError as exc:
                    db.execute("UPDATE yui_effects SET state='rejected',error=?,updated_at=? WHERE id=?",
                               (str(exc), self.clock(), effect_id))
                    return self._receipt(db.execute("SELECT * FROM yui_effects WHERE id=?", (effect_id,)).fetchone())
            db.execute("UPDATE yui_effects SET state=?,lease_until=?,updated_at=? WHERE id=?",
                       ("submitted" if first_submit else "needs_reconciliation",
                        self.clock() + self.lease_seconds, self.clock(), effect_id))
        receipt = self.get(effect_id)
        try:
            if first_submit:
                result = await asyncio.wait_for(
                    adapter.submit(receipt.payload, idempotency_key=receipt.id), self.submit_timeout,
                )
            elif adapter.capabilities.reconciliation:
                result = await asyncio.wait_for(adapter.reconcile(receipt), self.submit_timeout)
            else:
                result = None
            if result is None:
                return self._record(effect_id, state="needs_reconciliation", error="remote outcome is unconfirmed; submission will not be repeated")
            return self._record(effect_id, result=result)
        except asyncio.CancelledError:
            self._record(effect_id, state="needs_reconciliation", error="execution interrupted; remote outcome is unknown")
            raise
        except Exception as exc:
            return self._record(effect_id, state="needs_reconciliation", error=f"{type(exc).__name__}: {exc}"[:500])


class DiscordNotificationAdapter:
    """Receipts confirm message creation, never delivery to a human or alarm ringing."""

    capabilities = AdapterCapabilities(
        actions=("notify",), idempotency=False, reconciliation=True, cancellation=False,
        completion_semantics="Discord accepted the message and returned its message ID; reading or alarm ringing is unverified.",
        target_field="channel_id",
    )

    def __init__(self, client):
        self.client = client

    async def _channel(self, channel_id):
        channel = self.client.get_channel(int(channel_id))
        return channel if channel is not None else await self.client.fetch_channel(int(channel_id))

    @staticmethod
    def marker(effect_id):
        return "通知記録 " + effect_id

    async def submit(self, payload: dict, *, idempotency_key: str) -> AdapterResult:
        import discord

        channel = await self._channel(payload["channel_id"])
        embed = discord.Embed(description=str(payload["content"])[:3900])
        embed.set_footer(text=self.marker(idempotency_key))
        message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return AdapterResult(remote_id=str(message.id), result={
            "channel_id": str(channel.id), "message_id": str(message.id),
            "completion": "message_created", "read_confirmed": False,
        })

    async def reconcile(self, receipt: EffectReceipt) -> AdapterResult | None:
        from datetime import datetime, timezone

        channel = await self._channel(receipt.payload["channel_id"])
        if receipt.remote_id:
            message = await channel.fetch_message(int(receipt.remote_id))
            messages = [message]
        else:
            messages = [message async for message in channel.history(
                limit=100, after=datetime.fromtimestamp(receipt.created_at - 2, timezone.utc),
            )]
        own_id = getattr(getattr(self.client, "user", None), "id", None)
        for message in messages:
            if own_id is None or getattr(getattr(message, "author", None), "id", None) != own_id:
                continue
            if any(getattr(getattr(embed, "footer", None), "text", None) == self.marker(receipt.id)
                   for embed in getattr(message, "embeds", [])):
                return AdapterResult(remote_id=str(message.id), result={
                    "channel_id": str(channel.id), "message_id": str(message.id),
                    "completion": "message_created", "read_confirmed": False,
                    "reconciled": True,
                })
        return None

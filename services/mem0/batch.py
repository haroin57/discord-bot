"""Per-channel turn accumulator for batch Mem0 ingestion.

Instead of writing to Mem0 every turn, turns are accumulated on disk and
flushed as a single batch when the count reaches BATCH_SIZE (default 10).
This reduces Haiku extraction calls from N to 1 per batch window.

State files live under ``.state/mem0_batch/{channel_id}.json`` and survive
bot restarts. The next session picks up where the previous one left off.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable

from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

BATCH_SIZE = int(os.environ.get("MEM0_BATCH_SIZE", "10"))


def _batch_dir(state_dir: str | os.PathLike) -> Path:
    d = Path(state_dir) / "mem0_batch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _batch_file(state_dir: str | os.PathLike, channel_id: int) -> Path:
    return _batch_dir(state_dir) / f"{channel_id}.json"


def _load_raw(state_dir: str | os.PathLike, channel_id: int) -> dict:
    p = _batch_file(state_dir, channel_id)
    if not p.is_file():
        return {"turns": [], "kanae_facts": [], "cursor": 0}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"turns": [], "kanae_facts": [], "cursor": 0}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("batch load failed for ch=%d: %s", channel_id, exc)
        return {"turns": [], "kanae_facts": [], "cursor": 0}


def _save_raw(state_dir: str | os.PathLike, channel_id: int, data: dict) -> None:
    p = _batch_file(state_dir, channel_id)
    atomic_write_json(p, data)


def append_turn(
    state_dir: str | os.PathLike,
    channel_id: int,
    user_text: str,
    assistant_text: str,
    *,
    request_id: str | None = None,
) -> int:
    """Append a conversation turn and return the updated count."""
    turn = {
        "user": user_text[:2000],
        "assistant": assistant_text[:3000],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if request_id:
        turn["idempotency_key"] = str(request_id)
    return _batch_repository(state_dir).append_batch_turn(str(channel_id), turn)


def build_batch_summary(turns: list[dict], turn_chars: int | None = None) -> str:
    """Render accumulated turns into the ``user: …\\nassistant: …`` summary fed
    to the extractor. Each turn's user/assistant text is capped at
    ``turn_chars`` (default ``MEM0_BATCH_TURN_CHARS``). Kept here, pure and
    testable, instead of inline in bot.py so the truncation budget is one
    documented knob rather than a magic ``[:300]``.
    """
    if turn_chars is None:
        try:
            from config import MEM0_BATCH_TURN_CHARS

            turn_chars = MEM0_BATCH_TURN_CHARS
        except Exception:
            turn_chars = 600
    return "\n".join(
        f"user: {(t.get('user') or '')[:turn_chars]}\n"
        f"assistant: {(t.get('assistant') or '')[:turn_chars]}"
        for t in (turns or [])
    )


def append_kanae_facts(
    state_dir: str | os.PathLike,
    channel_id: int,
    facts: list[str],
) -> None:
    """Accumulate kanae-extracted facts for batch flush."""
    if not facts:
        return
    _batch_repository(state_dir).append_batch_facts(str(channel_id), facts)


@dataclass(frozen=True)
class BatchLease:
    """A durable batch claim that must be acknowledged or retried."""

    lease: object
    turns: list[dict]
    kanae_facts: list[str]
    state_dir: str = ""
    channel_id: str = ""
    lease_seconds: float = 30.0
    leased_at: float = 0.0

    def __iter__(self):
        """Preserve tuple unpacking without acknowledging the leased item."""
        if self.state_dir and self.channel_id:
            _register_compat_lease(self.state_dir, self.channel_id, self)
        yield self.turns
        yield self.kanae_facts


_CompatBaseKey = tuple[str, str]
_CompatRegistryKey = tuple[str, str, int, str]


@dataclass(frozen=True)
class _CompatClaim:
    work_id: int
    owner_task: str
    lease_token: str


@dataclass
class _CompatLeaseState:
    batch: BatchLease
    owner_task: str
    transferred: bool = False
    expiry_timer: threading.Timer | None = None


_compat_leases: dict[_CompatRegistryKey, _CompatLeaseState] = {}
_compat_leases_lock = threading.Lock()
_compat_claims: contextvars.ContextVar[dict[_CompatBaseKey, _CompatClaim] | None] = (
    contextvars.ContextVar("mem0_batch_compat_claims", default=None)
)
_compat_provider_tasks: set[asyncio.Task] = set()


def _compat_key(
    state_dir: str | os.PathLike,
    channel_id: int | str,
) -> _CompatBaseKey:
    return str(Path(state_dir).resolve()), str(channel_id)


def _compat_owner_task() -> str:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        return f"task:{id(task)}"
    return f"thread:{threading.get_ident()}"


def _compat_registry_key(
    base: _CompatBaseKey,
    work_id: int,
    lease_token: str,
) -> _CompatRegistryKey:
    return (*base, int(work_id), str(lease_token))


def _set_compat_claim(base: _CompatBaseKey, claim: _CompatClaim | None) -> None:
    claims = dict(_compat_claims.get() or {})
    if claim is None:
        claims.pop(base, None)
    else:
        claims[base] = claim
    _compat_claims.set(claims)


def _expire_compat_lease(
    base: _CompatBaseKey,
    work_id: int,
    lease_token: str,
    *,
    owner_task: str | None = None,
) -> None:
    key = _compat_registry_key(base, work_id, lease_token)
    with _compat_leases_lock:
        state = _compat_leases.get(key)
        if state is None or (owner_task is not None and state.owner_task != owner_task):
            return
        _compat_leases.pop(key, None)
        timer = state.expiry_timer
        state.expiry_timer = None
    if timer is not None:
        timer.cancel()


def _watch_compat_owner(
    base: _CompatBaseKey,
    work_id: int,
    lease_token: str,
    owner_task: str,
) -> None:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is None:
        return
    task.add_done_callback(
        lambda _task: _expire_compat_lease(
            base,
            work_id,
            lease_token,
            owner_task=owner_task,
        ),
    )


def _register_compat_lease(
    state_dir: str | os.PathLike,
    channel_id: int | str,
    batch: BatchLease,
) -> None:
    base = _compat_key(state_dir, channel_id)
    owner_task = _compat_owner_task()
    work_id = int(batch.lease.id)
    lease_token = str(batch.lease.token)
    key = _compat_registry_key(base, work_id, lease_token)
    expired_timers: list[threading.Timer] = []
    register_owner = False
    with _compat_leases_lock:
        state = _compat_leases.get(key)
        if state is not None:
            # Iterating the same BatchLease is idempotent only for the active
            # owner. Once ownership moved to the ingest task, the predecessor
            # cannot register the same object again and regain ACK authority.
            if state.owner_task != owner_task:
                return
        else:
            # A reclaimed work item has the same work id but a new lease
            # token. Registration of that successor invalidates every stale
            # predecessor entry for this channel before it can finish.
            for old_key, old_state in list(_compat_leases.items()):
                if old_key[:2] != base:
                    continue
                _compat_leases.pop(old_key, None)
                if old_state.expiry_timer is not None:
                    expired_timers.append(old_state.expiry_timer)
            remaining = max(
                0.0,
                batch.leased_at + batch.lease_seconds - time.monotonic(),
            )
            timer = threading.Timer(
                remaining,
                _expire_compat_lease,
                args=(base, work_id, lease_token),
            )
            timer.daemon = True
            state = _CompatLeaseState(
                batch=batch,
                owner_task=owner_task,
                expiry_timer=timer,
            )
            _compat_leases[key] = state
            register_owner = True
    for timer in expired_timers:
        timer.cancel()
    if register_owner and state.expiry_timer is not None:
        state.expiry_timer.start()
    claim = _CompatClaim(work_id, owner_task, lease_token)
    _set_compat_claim(base, claim)
    if register_owner:
        _watch_compat_owner(base, work_id, lease_token, owner_task)


def get_compat_batch_identity(
    state_dir: str | os.PathLike,
    channel_id: int | str,
) -> tuple[str, str] | None:
    """Return this task's exact compatibility owner and lease token."""
    base = _compat_key(state_dir, channel_id)
    identity = (_compat_claims.get() or {}).get(base)
    if identity is None:
        return None
    current_owner = _compat_owner_task()
    key = _compat_registry_key(base, identity.work_id, identity.lease_token)
    if identity.owner_task == current_owner:
        with _compat_leases_lock:
            state = _compat_leases.get(key)
            valid = state is not None and state.owner_task == current_owner
        if not valid:
            _set_compat_claim(base, None)
            return None
        return identity.owner_task, identity.lease_token

    # ``pop_batch`` is commonly tuple-unpacked by a parent task which then
    # creates the ingest task. Context variables carry the exact token into
    # that child; transfer registry ownership once so the child, not the
    # parent or a reclaimed predecessor, becomes the only compatible finisher.
    with _compat_leases_lock:
        state = _compat_leases.get(key)
        if (
            state is None
            or state.owner_task != identity.owner_task
            or state.transferred
        ):
            _set_compat_claim(base, None)
            return None
        state.owner_task = current_owner
        state.transferred = True
    new_claim = _CompatClaim(identity.work_id, current_owner, identity.lease_token)
    _set_compat_claim(base, new_claim)
    _watch_compat_owner(base, identity.work_id, identity.lease_token, current_owner)
    return current_owner, identity.lease_token


def current_compat_batch_claims() -> tuple[_CompatBaseKey, ...]:
    """Return raw inherited batch claims, including locally expired claims.

    An expired claim remains a batch-work signal until the durable queue
    formally issues a successor token. Callers must not reinterpret that
    context as an ordinary, unleased ingest request.
    """
    return tuple((_compat_claims.get() or {}).keys())


async def complete_cleanup(awaitable: Awaitable[Any]) -> Any:
    """Drain cleanup while preserving cancellation for the outer owner."""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _retain_provider_task(
    awaitable: Awaitable[Any],
    *,
    work_id: int,
) -> asyncio.Task:
    """Keep a batch provider operation alive until its terminal result."""
    task = asyncio.create_task(
        awaitable,
        name=f"mem0-batch-provider-{work_id}",
    )
    _compat_provider_tasks.add(task)

    def _consume_result(done: asyncio.Task) -> None:
        _compat_provider_tasks.discard(done)
        if done.cancelled():
            return
        done.exception()

    task.add_done_callback(_consume_result)
    return task


class CompatBatchOperation:
    """Fence one compatibility batch from provider call through disposition."""

    def __init__(self, batch: BatchLease) -> None:
        self.batch = batch
        self.repository = _batch_repository(batch.state_dir)
        self.lost = False
        self._heartbeat_task: asyncio.Task | None = None

    @property
    def idempotency_key(self) -> str:
        return str(self.batch.lease.key)

    def provider_task(self, awaitable: Awaitable[Any]) -> asyncio.Task:
        return _retain_provider_task(awaitable, work_id=int(self.batch.lease.id))

    async def _renew(self) -> bool:
        operation = asyncio.create_task(
            asyncio.to_thread(
                self.repository.renew_work,
                self.batch.lease,
                lease_seconds=self.batch.lease_seconds,
            ),
            name=f"mem0-batch-renew-{self.batch.lease.id}",
        )
        try:
            return bool(await asyncio.shield(operation))
        except asyncio.CancelledError:
            await complete_cleanup(operation)
            raise

    async def start(self) -> bool:
        if not await self._renew():
            self.lost = True
            return False
        interval = max(0.001, min(1.0, self.batch.lease_seconds / 3))
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(interval),
            name=f"mem0-batch-heartbeat-{self.batch.lease.id}",
        )
        return True

    async def fence(self) -> bool:
        if self.lost or not await self._renew():
            self.lost = True
            return False
        return True

    async def _heartbeat(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self._renew()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception(
                        "batch work lease renewal failed for %s: %s",
                        self.batch.lease.id,
                        exc,
                    )
                    renewed = False
                if not renewed:
                    self.lost = True
                    return
        except asyncio.CancelledError:
            raise

    async def finish(self, error: BaseException | str | None = None) -> bool | str:
        """ACK or retry only while this exact lease token is still live."""
        if self.lost:
            _forget_compat_lease(self.batch)
            return False
        try:
            if error is None:
                return await asyncio.to_thread(
                    ack_batch, self.batch.state_dir, self.batch
                )
            return await asyncio.to_thread(
                retry_batch,
                self.batch.state_dir,
                self.batch,
                error,
            )
        finally:
            # ack_batch/retry_batch run in a worker-thread Context. Clear the
            # exact claim again in the async caller's Context so subsequent
            # ordinary ingests in this task are not reinterpreted as batch work.
            _forget_compat_claim(self.batch)

    async def close(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def begin_compat_batch(
    state_dir: str | os.PathLike,
    channel_id: int | str,
) -> CompatBatchOperation | None:
    """Claim the caller's compatibility lease and suspend its expiry timer."""
    identity = get_compat_batch_identity(state_dir, channel_id)
    if identity is None:
        return None
    base = _compat_key(state_dir, channel_id)
    owner_task, lease_token = identity
    with _compat_leases_lock:
        matches = [
            state
            for key, state in _compat_leases.items()
            if key[:2] == base
            and key[3] == str(lease_token)
            and state.owner_task == owner_task
        ]
        if len(matches) != 1:
            return None
        state = matches[0]
        timer = state.expiry_timer
        state.expiry_timer = None
    if timer is not None:
        timer.cancel()
    return CompatBatchOperation(state.batch)


def _forget_compat_lease(batch: BatchLease) -> None:
    removed_bases: set[_CompatBaseKey] = set()
    timers: list[threading.Timer] = []
    with _compat_leases_lock:
        for key, state in list(_compat_leases.items()):
            current = state.batch
            if (
                current.lease.id == batch.lease.id
                and current.lease.owner == batch.lease.owner
                and current.lease.token == batch.lease.token
            ):
                _compat_leases.pop(key, None)
                removed_bases.add((key[0], key[1]))
                if state.expiry_timer is not None:
                    timers.append(state.expiry_timer)
    for timer in timers:
        timer.cancel()
    removed_bases.add(_compat_key(batch.state_dir, batch.channel_id))
    _forget_compat_claim(batch, bases=removed_bases)


def _forget_compat_claim(
    batch: BatchLease,
    *,
    bases: set[_CompatBaseKey] | None = None,
) -> None:
    """Remove only the caller-context claim matching this durable token."""
    if bases is None:
        bases = {_compat_key(batch.state_dir, batch.channel_id)}
    claims = dict(_compat_claims.get() or {})
    for base, claim in list(claims.items()):
        if (
            base in bases
            and claim.work_id == int(batch.lease.id)
            and claim.lease_token == str(batch.lease.token)
        ):
            claims.pop(base, None)
    _compat_claims.set(claims)


def _batch_repository(state_dir: str | os.PathLike):
    from services.mem0.integrity import get_repository

    return get_repository(Path(state_dir) / "mem0_canonical.sqlite")


def lease_batch(
    state_dir: str | os.PathLike,
    channel_id: int,
    *,
    worker_id: str | None = None,
    lease_seconds: float = 30.0,
    max_attempts: int = 5,
) -> BatchLease | None:
    """Move the legacy accumulator into a leased durable work item.

    Enqueue is idempotent. A crash after enqueue but before clearing the JSON
    accumulator recreates the same ``job_id`` and does not duplicate work.
    """
    repository = _batch_repository(state_dir)
    data = _load_raw(state_dir, channel_id)
    turns = list(data.get("turns") or [])
    facts = list(data.get("kanae_facts") or [])
    if turns or facts:
        cursor = str(data.get("cursor") or len(turns) + len(facts))
        payload = {"turns": turns, "kanae_facts": facts}
        repository.enqueue_work(
            "batch",
            str(channel_id),
            cursor,
            payload,
            max_attempts=max_attempts,
        )
        _save_raw(
            state_dir,
            channel_id,
            {"turns": [], "kanae_facts": [], "cursor": int(cursor)},
        )

    owner = worker_id or f"batch-{os.getpid()}-{uuid.uuid4().hex}"
    lease = repository.finalize_and_lease_batch(
        str(channel_id),
        owner,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    if lease is None:
        return None
    return BatchLease(
        lease=lease,
        turns=list(lease.payload.get("turns") or []),
        kanae_facts=list(lease.payload.get("kanae_facts") or []),
        state_dir=str(Path(state_dir).resolve()),
        channel_id=str(channel_id),
        lease_seconds=lease_seconds,
        leased_at=time.monotonic(),
    )


def ack_batch(state_dir: str | os.PathLike, batch: BatchLease) -> bool:
    acked = _batch_repository(state_dir).ack_work(batch.lease)
    _forget_compat_lease(batch)
    return acked


def retry_batch(
    state_dir: str | os.PathLike,
    batch: BatchLease,
    error: BaseException | str,
    *,
    retry_seconds: float = 1.0,
) -> str:
    status = _batch_repository(state_dir).retry_work(
        batch.lease,
        error,
        retry_seconds=retry_seconds,
    )
    _forget_compat_lease(batch)
    return status


def finish_compat_batch(
    state_dir: str | os.PathLike,
    channel_id: int | str,
    *,
    error: BaseException | str | None = None,
    owner_task: str | None = None,
    lease_token: str | None = None,
) -> bool | str:
    """Complete a tuple-unpacked compatibility lease after ingest finishes."""
    base = _compat_key(state_dir, channel_id)
    if owner_task is None or lease_token is None:
        identity = get_compat_batch_identity(state_dir, channel_id)
        if identity is None:
            return False
        owner_task, lease_token = identity
    with _compat_leases_lock:
        matches = [
            state
            for key, state in _compat_leases.items()
            if key[:2] == base
            and key[3] == str(lease_token)
            and state.owner_task == owner_task
        ]
    if len(matches) != 1:
        return False
    batch = matches[0].batch
    if error is None:
        return ack_batch(state_dir, batch)
    return retry_batch(state_dir, batch, error)


def pop_batch(
    state_dir: str | os.PathLike,
    channel_id: int,
) -> BatchLease | None:
    """Lease a batch without acknowledging it.

    Tuple unpacking remains available through ``BatchLease.__iter__``, but the
    caller must retain the lease and explicitly acknowledge or retry it.
    """
    batch = lease_batch(state_dir, channel_id)
    return batch

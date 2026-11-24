"""Durable Bot turn commit journal keyed by correlation request ID."""

from __future__ import annotations

import asyncio
import copy
import contextvars
import functools
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

_VERSION = 1
_TERMINAL_LIMIT = 256


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _snowflake(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class TurnOutbox:
    """Atomic JSON journal for delivery/archive/session/record reconciliation."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        # Async locks belong to the event loop, not the journal worker. Weak
        # entries live while holders/waiters reference them and cannot grow
        # forever or be evicted underneath an active reconciliation.
        self._reconcile_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._state = self._load()
        self._dirty = False

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as file:
                state = json.load(file)
        except FileNotFoundError:
            return {"version": _VERSION, "turns": {}}
        except (OSError, json.JSONDecodeError) as exc:
            log.error("turn outbox is unreadable; starting fail-closed: %s", exc)
            return {"version": _VERSION, "turns": {}}
        if state.get("version") != _VERSION or not isinstance(state.get("turns"), dict):
            log.error("turn outbox has unsupported format; starting fail-closed")
            return {"version": _VERSION, "turns": {}}
        return state

    def _save(self) -> None:
        self._dirty = True
        atomic_write_json(self.path, self._state)
        self._dirty = False

    def _flush_pending(self) -> None:
        with self._lock:
            if self._dirty:
                self._save()

    async def run_io(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run journal I/O off-loop without leaving a cancelled write behind.

        A receipt must reach durable storage before cancellation cleanup can
        inspect or discard the turn. Shield and drain the worker even when a
        second cancellation arrives; a thread cannot be cancelled safely.
        """
        # Use a Future rather than a Task wrapping to_thread. Event-loop
        # shutdown cancels every Task; cancelling a to_thread Task would mark
        # it done before the real thread has persisted an acknowledged turn.
        context = contextvars.copy_context()
        worker = asyncio.get_running_loop().run_in_executor(
            None, functools.partial(context.run, operation, *args, **kwargs)
        )
        cancellation = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
            except Exception:
                break
        if cancellation is not None:
            try:
                worker.result()
            except Exception:
                log.exception("turn outbox I/O failed during cancellation")
            raise cancellation
        return worker.result()

    def begin(
        self,
        request_id: str,
        *,
        request_message_id: int,
        channel_id: int,
        user_id: int,
        static_payload: dict[str, Any] | None = None,
        archive_intent: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if request_id in self._state["turns"]:
                self._flush_pending()
                return
            self._state["turns"].setdefault(
                request_id,
                {
                    "request_id": request_id,
                    "request_message_id": _snowflake(request_message_id),
                    "channel_id": _snowflake(channel_id),
                    "user_id": _snowflake(user_id),
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "delivery": {"message_ids": [], "streamed": False},
                    "payload": _json_safe(static_payload),
                    "payload_complete": False,
                    "archive_intent": _json_safe(archive_intent),
                    "archive_jobs": [],
                    "steps": {
                        "archive": "pending",
                        "session": "pending",
                        "record": "pending",
                    },
                },
            )
            self._save()

    def stage_request(
        self,
        request_id: str,
        *,
        request_message_id: int,
        channel_id: int,
        user_id: int,
        static_payload: dict[str, Any],
        archive_intent: dict[str, Any],
    ) -> None:
        """Atomically stage the immutable request and intended output class."""
        self.begin(
            request_id,
            request_message_id=request_message_id,
            channel_id=channel_id,
            user_id=user_id,
            static_payload=static_payload,
            archive_intent=archive_intent,
        )

    def prepare(self, request_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            turn = self._state["turns"][request_id]
            current = turn.get("payload")
            if not isinstance(current, dict):
                current = {}
            current.update(_json_safe(payload))
            turn["payload"] = current
            turn["payload_complete"] = True
            turn.pop("terminal_state", None)
            for step in ("session", "record"):
                if turn.get("steps", {}).get(step) == "skipped":
                    turn["steps"][step] = "pending"
            turn["updated_at"] = time.time()
            self._save()

    def record_delivery(
        self,
        request_id: str,
        *,
        message_ids: list[int],
        streamed: bool,
    ) -> None:
        with self._lock:
            turn = self._state["turns"].get(request_id)
            if turn is None:
                return
            receipt = turn["delivery"]
            changed = not receipt.get("acknowledged")
            for message_id in message_ids:
                if message_id not in receipt["message_ids"]:
                    receipt["message_ids"].append(message_id)
                    changed = True
            changed = changed or (streamed and not receipt["streamed"])
            if not changed:
                self._flush_pending()
                return
            receipt["streamed"] = bool(receipt["streamed"] or streamed)
            receipt["acknowledged"] = True
            turn["updated_at"] = time.time()
            self._save()

    def add_archive_job(
        self,
        request_id: str,
        *,
        paths: list[str],
        kwargs: dict[str, Any],
        response_message_id: int | None = None,
        bind_response: bool = True,
    ) -> None:
        if not paths:
            return
        with self._lock:
            turn = self._state["turns"].get(request_id)
            if turn is None:
                return
            safe_paths = _json_safe(paths)
            safe_kwargs = _json_safe(kwargs)
            safe_response_id = (
                response_message_id if isinstance(response_message_id, int) else None
            )
            key = [safe_paths, copy.deepcopy(safe_kwargs), bind_response]
            existing = next(
                (job for job in turn["archive_jobs"] if job.get("key") == key),
                None,
            )
            if existing is None:
                staged_paths, cleanup_paths = self._stage_archive_paths(
                    request_id,
                    safe_paths,
                )
                per_path_metadata = safe_kwargs.get("per_path_metadata")
                if isinstance(per_path_metadata, dict):
                    safe_kwargs["per_path_metadata"] = {
                        staged: per_path_metadata.get(source, {})
                        for source, staged in zip(safe_paths, staged_paths, strict=True)
                    }
                archive_job = {
                    "key": key,
                    "paths": staged_paths,
                    "source_paths": safe_paths,
                    "cleanup_paths": cleanup_paths,
                    "kwargs": safe_kwargs,
                    "response_message_id": safe_response_id,
                    "bind_response": bind_response,
                    "status": "pending",
                }
                key_payload = json.dumps(
                    key,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                archive_job["idempotency_key"] = (
                    f"{request_id}:archive:"
                    f"{hashlib.sha256(key_payload.encode()).hexdigest()[:24]}"
                )
                turn["archive_jobs"].append(archive_job)
                turn["steps"]["archive"] = "pending"
            elif safe_response_id is not None:
                existing["response_message_id"] = safe_response_id
            turn["updated_at"] = time.time()
            self._save()

    def _stage_archive_paths(
        self,
        request_id: str,
        paths: list[str],
    ) -> tuple[list[str], list[str]]:
        artifact_root = f"{self.path}.artifacts"
        turn_dir = os.path.join(
            artifact_root,
            hashlib.sha256(request_id.encode()).hexdigest()[:24],
        )
        staged: list[str] = []
        cleanup: list[str] = []
        for index, source in enumerate(paths):
            if not isinstance(source, str) or not os.path.isfile(source):
                staged.append(source)
                continue
            os.makedirs(turn_dir, mode=0o700, exist_ok=True)
            suffix = os.path.splitext(source)[1][:16]
            destination = os.path.join(
                turn_dir,
                f"{index:02d}-{hashlib.sha256(source.encode()).hexdigest()[:16]}{suffix}",
            )
            part = f"{destination}.part"
            try:
                shutil.copyfile(source, part)
                os.chmod(part, 0o600)
                os.replace(part, destination)
            finally:
                try:
                    os.remove(part)
                except OSError:
                    pass
            staged.append(destination)
            cleanup.append(destination)
        return staged, cleanup

    def snapshot(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            turn = self._state["turns"].get(request_id)
            return copy.deepcopy(turn) if turn is not None else None

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                copy.deepcopy(turn)
                for turn in self._state["turns"].values()
                if turn.get("delivery", {}).get("acknowledged")
                and not self._is_terminal(turn)
            ]

    @staticmethod
    def _is_terminal(turn: dict[str, Any]) -> bool:
        return all(
            value in {"done", "skipped"} for value in turn.get("steps", {}).values()
        ) and all(job.get("status") == "done" for job in turn.get("archive_jobs", []))

    @staticmethod
    def _payload_complete(turn: dict[str, Any]) -> bool:
        marker = turn.get("payload_complete")
        if marker is None:
            # Version-1 journals written before static staging only persisted
            # complete payloads. Preserve their restart contract.
            return turn.get("payload") is not None
        return marker is True

    def discard_unacknowledged(self, request_id: str) -> None:
        with self._lock:
            turn = self._state["turns"].get(request_id)
            if turn is None or turn.get("delivery", {}).get("acknowledged"):
                return
            self._cleanup_turn_artifacts(turn)
            self._state["turns"].pop(request_id, None)
            self._save()

    @staticmethod
    def _cleanup_turn_artifacts(turn: dict[str, Any]) -> None:
        for job in turn.get("archive_jobs", []):
            for path in job.get("cleanup_paths", []):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _mark(self, request_id: str, step: str, status: str) -> None:
        with self._lock:
            turn = self._state["turns"].get(request_id)
            if turn is None:
                return
            if turn["steps"].get(step) == status:
                self._flush_pending()
                return
            turn["steps"][step] = status
            turn["updated_at"] = time.time()
            self._save()

    def _mark_archive_job(
        self,
        request_id: str,
        key: list[Any],
        status: str,
    ) -> None:
        with self._lock:
            turn = self._state["turns"].get(request_id)
            if turn is None:
                return
            for job in turn["archive_jobs"]:
                if job.get("key") == key:
                    job["status"] = status
                    if status == "done":
                        for path in job.get("cleanup_paths", []):
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                        job["cleanup_paths"] = []
                    break
            self._save()

    async def reconcile(
        self,
        request_id: str,
        *,
        archive: Callable[[dict[str, Any]], Awaitable[None]],
        session: Callable[[dict[str, Any]], Awaitable[None]],
        record: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> bool:
        """Apply every idempotent step, retaining failures for restart replay."""
        reconcile_lock = self._reconcile_locks.setdefault(request_id, asyncio.Lock())
        async with reconcile_lock:
            return await self._reconcile_locked(
                request_id,
                archive=archive,
                session=session,
                record=record,
            )

    async def _reconcile_locked(
        self,
        request_id: str,
        *,
        archive: Callable[[dict[str, Any]], Awaitable[None]],
        session: Callable[[dict[str, Any]], Awaitable[None]],
        record: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> bool:
        turn = await self.run_io(self.snapshot, request_id)
        if turn is None or not turn.get("delivery", {}).get("acknowledged"):
            return False
        if self._is_terminal(turn):
            await self.run_io(self._flush_pending)
            return True
        callbacks = {"session": session, "record": record}
        failed = False

        pending_jobs = [
            job for job in turn["archive_jobs"] if job.get("status") != "done"
        ]
        for job in pending_jobs:
            if (
                job.get("bind_response", True)
                and job.get("response_message_id") is None
            ):
                delivered_ids = turn.get("delivery", {}).get("message_ids", [])
                if delivered_ids:
                    job["response_message_id"] = delivered_ids[0]
            if not job.get("idempotency_key"):
                job["idempotency_key"] = f"{request_id}:archive"
            await self.run_io(self._mark_archive_job, request_id, job["key"], "applying")
            try:
                await archive(job)
            except Exception as exc:
                failed = True
                await self.run_io(self._mark_archive_job, request_id, job["key"], "pending")
                log.warning("turn outbox archive reconcile failed: %s", exc)
            else:
                await self.run_io(self._mark_archive_job, request_id, job["key"], "done")
        latest = await self.run_io(self.snapshot, request_id)
        if latest is not None and all(
            job.get("status") == "done" for job in latest["archive_jobs"]
        ):
            await self.run_io(self._mark, request_id, "archive", "done")

        latest = await self.run_io(self.snapshot, request_id)
        if latest is None:
            return False
        if not self._payload_complete(latest) and await self.run_io(
            self._mark_partial_if_incomplete, request_id
        ):
            # A stream can be acknowledged before provider completion. A
            # crash/cancellation then has no response/session payload to
            # commit. Retain the delivery receipt, finish archive intent, and
            # explicitly terminalize the unavailable steps for restart replay.
            await self.run_io(self._prune_terminal)
            return not failed

        for step, callback in callbacks.items():
            latest = await self.run_io(self.snapshot, request_id)
            if latest is None or latest["steps"].get(step) == "done":
                continue
            await self.run_io(self._mark, request_id, step, "applying")
            latest["idempotency_key"] = request_id
            latest["idempotency_step"] = step
            try:
                await callback(latest)
            except Exception as exc:
                failed = True
                await self.run_io(self._mark, request_id, step, "pending")
                log.warning("turn outbox %s reconcile failed: %s", step, exc)
            else:
                await self.run_io(self._mark, request_id, step, "done")

        await self.run_io(self._prune_terminal)
        return not failed

    def _mark_partial_if_incomplete(self, request_id: str) -> bool:
        """Atomically recheck provider completion before terminalizing a stream.

        prepare() may complete between asynchronous snapshots. It either wins
        this lock and prevents skipping, or runs later and reopens both steps.
        """
        with self._lock:
            current = self._state["turns"].get(request_id)
            if current is None or self._payload_complete(current):
                return False
            changed = current.get("terminal_state") != "partial"
            for step in ("session", "record"):
                if current.get("steps", {}).get(step) not in {"done", "skipped"}:
                    current["steps"][step] = "skipped"
                    changed = True
            current["terminal_state"] = "partial"
            if changed or self._dirty:
                current["updated_at"] = time.time()
                self._save()
            return True

    def _prune_terminal(self) -> None:
        with self._lock:
            terminal = [
                (request_id, turn.get("updated_at", 0.0))
                for request_id, turn in self._state["turns"].items()
                if self._is_terminal(turn)
            ]
            expired = sorted(terminal, key=lambda item: item[1])[:-_TERMINAL_LIMIT]
            for request_id, _ in expired:
                self._state["turns"].pop(request_id, None)
            if expired or self._dirty:
                self._save()

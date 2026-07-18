"""Single-owner session state and durable persistence.

The registry owns all process-local Claude session mappings and provider
continuity invalidations.  The repository owns the on-disk representation.
Compatibility functions remain in ``services.session``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Literal, NoReturn, TypeVar, overload

from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

ProviderName = Literal["claude", "codex"]
SessionKey = tuple[int, int | None]
ContinuityKey = tuple[ProviderName, int, int | None]
_FORMAT_VERSION = 2
_MISSING = object()
_DefaultT = TypeVar("_DefaultT")


@dataclass
class SessionInfo:
    session_id: str
    mcp_server_keys: list[str] = field(default_factory=list)
    # Runtime-only usage data is intentionally not persisted.
    last_usage: dict | None = None


def _copy_session_info(info: SessionInfo) -> SessionInfo:
    """Return a registry boundary copy of mutable session metadata."""
    return SessionInfo(
        session_id=info.session_id,
        mcp_server_keys=list(info.mcp_server_keys),
        last_usage=copy.deepcopy(info.last_usage),
    )


@dataclass(frozen=True, slots=True)
class SessionRecord:
    channel_id: int
    user_id: int | None
    session_id: str
    mcp_server_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    revision: int
    sessions: tuple[SessionRecord, ...]
    invalidated_providers: frozenset[ContinuityKey]
    invalidate_all_provider_continuity: bool = False
    validated_providers: frozenset[ContinuityKey] = frozenset()
    needs_rewrite: bool = field(default=False, compare=False, repr=False)


class SessionRepository:
    """Load and atomically save immutable registry snapshots."""

    def __init__(self, path: str | Callable[[], str]) -> None:
        self._path = path

    @property
    def path(self) -> str:
        return self._path() if callable(self._path) else self._path

    def load(self) -> RegistrySnapshot:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return RegistrySnapshot(0, (), frozenset())
        except json.JSONDecodeError as exc:
            log.error(
                "session registry JSON is corrupt; invalidating all continuity: %s", exc
            )
            return self._fail_closed_snapshot()

        if not isinstance(data, dict):
            log.error(
                "session registry root is not an object; invalidating all continuity"
            )
            return self._fail_closed_snapshot()
        if "version" not in data:
            return self._load_legacy(data)
        if data.get("version") != _FORMAT_VERSION:
            log.error(
                "unsupported session registry version %r; invalidating all continuity",
                data.get("version"),
            )
            return self._fail_closed_snapshot()
        try:
            return self._load_v2(data)
        except (KeyError, TypeError, ValueError) as exc:
            log.error(
                "session registry payload is invalid; invalidating all continuity: %s",
                exc,
            )
            return self._fail_closed_snapshot()

    def _load_v2(self, data: dict) -> RegistrySnapshot:
        raw_sessions = data.get("sessions", [])
        raw_invalidated = data.get("invalidated_providers", [])
        raw_validated = data.get("validated_providers", [])
        if not isinstance(raw_sessions, list):
            raise TypeError("sessions must be a list")
        if not isinstance(raw_invalidated, list):
            raise TypeError("invalidated_providers must be a list")
        if not isinstance(raw_validated, list):
            raise TypeError("validated_providers must be a list")

        records: list[SessionRecord] = []
        for value in raw_sessions:
            if not isinstance(value, dict):
                raise TypeError("session record must be an object")
            channel_id = int(value["channel_id"])
            raw_user_id = value.get("user_id")
            user_id = None if raw_user_id is None else int(raw_user_id)
            session_id = value["session_id"]
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("session_id must be a non-empty string")
            raw_keys = value.get("mcp_server_keys", [])
            if raw_keys is None:
                raw_keys = []
            if not isinstance(raw_keys, list):
                raise TypeError("mcp_server_keys must be a list")
            keys = tuple(str(key) for key in raw_keys if isinstance(key, str))
            records.append(SessionRecord(channel_id, user_id, session_id, keys))

        invalidated = self._load_continuity_keys(raw_invalidated)
        validated = self._load_continuity_keys(raw_validated)

        revision = max(0, int(data.get("revision", 0)))
        invalidate_all = data.get("invalidate_all_provider_continuity", False)
        if not isinstance(invalidate_all, bool):
            raise TypeError("invalidate_all_provider_continuity must be a boolean")
        if not invalidate_all and validated:
            raise ValueError(
                "validated_providers requires global continuity invalidation"
            )
        return RegistrySnapshot(
            revision,
            tuple(records),
            frozenset(invalidated),
            invalidate_all,
            frozenset(validated),
        )

    def _load_legacy(self, data: dict) -> RegistrySnapshot:
        # Legacy files have channel-only sessions and therefore cannot identify
        # the user who owns the conversation. Reusing them for the first user
        # after upgrade can disclose another user's context. Migrate by
        # discarding every legacy session and forcing one provider cleanup per
        # concrete (provider, channel, user) before continuity can resume.
        if data:
            log.warning(
                "discarding legacy channel-only sessions and invalidating all provider continuity"
            )
        return RegistrySnapshot(
            0,
            (),
            frozenset(),
            True,
            frozenset(),
            needs_rewrite=True,
        )

    @staticmethod
    def _load_continuity_keys(values: list) -> set[ContinuityKey]:
        result: set[ContinuityKey] = set()
        for value in values:
            if not isinstance(value, dict):
                raise TypeError("continuity record must be an object")
            provider = value.get("provider")
            if provider not in ("claude", "codex"):
                raise ValueError("unsupported provider in continuity record")
            channel_id = int(value["channel_id"])
            raw_user_id = value.get("user_id")
            user_id = None if raw_user_id is None else int(raw_user_id)
            result.add((provider, channel_id, user_id))
        return result

    @staticmethod
    def _fail_closed_snapshot() -> RegistrySnapshot:
        return RegistrySnapshot(
            0,
            (),
            frozenset(),
            True,
            frozenset(),
            needs_rewrite=True,
        )

    def save(self, snapshot: RegistrySnapshot) -> None:
        data = {
            "version": _FORMAT_VERSION,
            "revision": snapshot.revision,
            "sessions": [
                {
                    "channel_id": record.channel_id,
                    "user_id": record.user_id,
                    "session_id": record.session_id,
                    "mcp_server_keys": list(record.mcp_server_keys),
                }
                for record in snapshot.sessions
            ],
            "invalidated_providers": [
                {"provider": provider, "channel_id": channel_id, "user_id": user_id}
                for provider, channel_id, user_id in sorted(
                    snapshot.invalidated_providers,
                    key=lambda value: (value[0], value[1], value[2] or -1),
                )
            ],
            "invalidate_all_provider_continuity": (
                snapshot.invalidate_all_provider_continuity
            ),
            "validated_providers": [
                {"provider": provider, "channel_id": channel_id, "user_id": user_id}
                for provider, channel_id, user_id in sorted(
                    snapshot.validated_providers,
                    key=lambda value: (
                        value[0],
                        value[1],
                        -1 if value[2] is None else value[2],
                    ),
                )
            ],
        }
        atomic_write_json(self.path, data)


class SessionRegistryLoadError(RuntimeError):
    """The durable registry could not be loaded for this process lifecycle."""


class SessionRegistryClosedError(RuntimeError):
    """The registry lifecycle has ended and cannot accept new work."""


class _ChannelSessionView(MutableMapping[int, SessionInfo]):
    """Legacy channel-only mapping backed by the registry, not a second store."""

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    def __getitem__(self, channel_id: int) -> SessionInfo:
        return self._registry.channel_projection()[channel_id]

    def __setitem__(self, channel_id: int, info: SessionInfo) -> None:
        self._registry._ensure_mutable()
        self._registry.set(channel_id, None, info)

    def __delitem__(self, channel_id: int) -> None:
        self._registry._ensure_mutable()
        if not self._registry.clear(channel_id, None):
            raise KeyError(channel_id)

    def __iter__(self) -> Iterator[int]:
        return iter(self._registry.channel_projection())

    def __len__(self) -> int:
        return len(self._registry.channel_projection())

    def clear(self) -> None:
        self._registry._ensure_mutable()
        while self:
            self.popitem()

    @overload
    def pop(self, channel_id: int, /) -> SessionInfo: ...

    @overload
    def pop(
        self,
        channel_id: int,
        default: SessionInfo,
        /,
    ) -> SessionInfo: ...

    @overload
    def pop(
        self,
        channel_id: int,
        default: _DefaultT,
        /,
    ) -> SessionInfo | _DefaultT: ...

    def pop(
        self,
        channel_id: int,
        default: object = _MISSING,
        /,
    ) -> SessionInfo | object:
        self._registry._ensure_mutable()
        try:
            info = self[channel_id]
        except KeyError:
            if default is _MISSING:
                raise
            return default
        del self[channel_id]
        return info

    def popitem(self, last: bool = True) -> tuple[int, SessionInfo]:
        self._registry._ensure_mutable()
        projection = self._registry.channel_projection()
        if not projection:
            raise KeyError("dictionary is empty")
        channel_id = next(reversed(projection)) if last else next(iter(projection))
        info = projection[channel_id]
        del self[channel_id]
        return channel_id, info

    @overload
    def setdefault(
        self,
        channel_id: int,
        default: SessionInfo,
        /,
    ) -> SessionInfo: ...

    @overload
    def setdefault(
        self,
        channel_id: int,
        default: None = None,
        /,
    ) -> None: ...

    def setdefault(
        self,
        channel_id: int,
        default: SessionInfo | None = None,
        /,
    ) -> SessionInfo | None:
        self._registry._ensure_mutable()
        try:
            return self[channel_id]
        except KeyError:
            # ``None`` is not a valid SessionInfo and retains the legacy
            # MutableMapping failure behavior when no default is supplied.
            if default is None:
                raise TypeError("session default must be SessionInfo")
            self[channel_id] = default
            return default

    def update(self, other=(), /, **kwargs: SessionInfo) -> None:
        self._registry._ensure_mutable()
        super().update(other, **kwargs)

    def __ior__(self, other) -> _ChannelSessionView:
        self._registry._ensure_mutable()
        self.update(other)
        return self

    def move_to_end(self, channel_id: int, last: bool = True) -> None:
        self._registry._ensure_mutable()
        key = (int(channel_id), None)
        if key not in self._registry._sessions:
            raise KeyError(channel_id)
        self._registry._sessions.move_to_end(key, last=last)


class SessionRegistry:
    """Own session state and serialize all asynchronous repository writes.

    Snapshot and lookup methods remain readable after close. Every public
    mutation and writer entry point rejects work after close; ``close`` itself
    only drains snapshots accepted before it established the close fence.
    """

    def __init__(self, repository: SessionRepository, *, max_sessions: int) -> None:
        self.repository = repository
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[SessionKey, SessionInfo] = OrderedDict()
        self._invalidated_providers: set[ContinuityKey] = set()
        self._invalidate_all_provider_continuity = False
        self._validated_providers: set[ContinuityKey] = set()
        self._revision = 0
        self._pending_snapshot: RegistrySnapshot | None = None
        self._save_task: asyncio.Task[None] | None = None
        self._save_tasks: set[asyncio.Task] = set()
        self._last_save_error: Exception | None = None
        self._loaded = False
        self._load_task: asyncio.Task[RegistrySnapshot] | None = None
        self._load_owner: object | None = None
        self._load_error: Exception | None = None
        self._load_failure: SessionRegistryLoadError | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._close_error: Exception | None = None
        self._closed = False
        self._epoch = 0
        self._repository_load_lock = threading.Lock()
        self._repository_save_lock = threading.Lock()
        self._channel_view = _ChannelSessionView(self)

    @property
    def channel_view(self) -> _ChannelSessionView:
        return self._channel_view

    def load(self, *, force: bool = False) -> RegistrySnapshot:
        return self._load(force=force, owner=None)

    def _load(self, *, force: bool, owner: object | None) -> RegistrySnapshot:
        if not self._load_is_admitted(owner):
            raise SessionRegistryClosedError("session registry is closed")
        if self._load_error is not None:
            raise self._load_error

        # The lock also covers the second loaded check, so a synchronous
        # bootstrap load racing the workflow's async first use still performs
        # exactly one repository read.
        with self._repository_load_lock:
            if not self._load_is_admitted(owner):
                raise SessionRegistryClosedError("session registry is closed")
            if self._loaded and not force:
                return self.snapshot()
            if self._load_error is not None:
                raise self._load_error
            if force and self._loaded:
                # A forced disk reload is a lifecycle boundary. Persist the newest
                # in-memory revision under the repository lock, advance the epoch
                # so the old drain cannot resume, then read it back.
                self.flush_sync()
            elif force:
                self._epoch += 1
            try:
                snapshot = self.repository.load()
                if not self._load_is_admitted(owner):
                    raise SessionRegistryClosedError("session registry is closed")
                self._sessions.clear()
                for record in snapshot.sessions:
                    self._sessions[(record.channel_id, record.user_id)] = SessionInfo(
                        session_id=record.session_id,
                        mcp_server_keys=list(record.mcp_server_keys),
                    )
                self._invalidated_providers = set(snapshot.invalidated_providers)
                self._invalidate_all_provider_continuity = (
                    snapshot.invalidate_all_provider_continuity
                )
                self._validated_providers = set(snapshot.validated_providers)
                self._revision = snapshot.revision
                self._loaded = True
                self._enforce_limit()
                if snapshot.needs_rewrite:
                    self._revision += 1
                    # A first-use load admitted before close owns this rewrite.
                    # It must not be rejected by the public close mutation fence.
                    self._request_save()
            except SessionRegistryClosedError:
                raise
            except Exception as exc:
                if self._load_error is None:
                    self._load_error = exc
                raise
            self._load_error = None
            self._load_failure = None
            return self.snapshot()

    async def ensure_loaded(self) -> RegistrySnapshot:
        """Load durable state once without blocking the event loop.

        All concurrent first users share one shielded task. A repository error
        remains sticky for the lifecycle so no later request can dispatch from
        an empty in-memory registry and overwrite the durable V2 state.
        """

        if self._closed:
            raise SessionRegistryClosedError("session registry is closed")
        if self._load_error is not None:
            self._raise_sticky_load_error()
        if self._loaded:
            return self.snapshot()

        task = self._load_task
        if task is None:
            owner = object()
            self._load_owner = owner
            task = asyncio.create_task(
                asyncio.to_thread(self._load_owned, owner),
                name="session-registry-first-use-load",
            )
            self._load_task = task
            task.add_done_callback(self._load_done)

        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation belongs to this waiter, not the shared load.
            raise
        except SessionRegistryClosedError:
            raise
        except Exception as exc:
            if self._load_error is None:
                self._load_error = exc
            self._raise_sticky_load_error()

    def snapshot(self) -> RegistrySnapshot:
        records = tuple(
            SessionRecord(
                channel_id, user_id, info.session_id, tuple(info.mcp_server_keys)
            )
            for (channel_id, user_id), info in self._sessions.items()
        )
        return RegistrySnapshot(
            self._revision,
            records,
            frozenset(self._invalidated_providers),
            self._invalidate_all_provider_continuity,
            frozenset(self._validated_providers),
        )

    def get(self, channel_id: int, user_id: int | None) -> SessionInfo | None:
        if self.is_provider_invalid("claude", channel_id, user_id):
            return None
        info = self._sessions.get((channel_id, user_id))
        return None if info is None else _copy_session_info(info)

    def set(self, channel_id: int, user_id: int | None, info: SessionInfo) -> None:
        self._ensure_mutable()
        key = (int(channel_id), None if user_id is None else int(user_id))
        continuity_key: ContinuityKey = ("claude", *key)
        stored_info = _copy_session_info(info)
        if self._sessions.get(key) == stored_info and not self.is_provider_invalid(
            "claude", *key
        ):
            self._sessions.move_to_end(key)
            return
        self._sessions[key] = stored_info
        self._sessions.move_to_end(key)
        self._invalidated_providers.discard(continuity_key)
        if self._invalidate_all_provider_continuity:
            self._validated_providers.add(continuity_key)
        self._enforce_limit()
        self._changed()

    def replace_channel_sessions(
        self,
        sessions: MutableMapping[int, SessionInfo],
    ) -> None:
        """Replace session mappings through the registry-owned writer.

        Provider invalidations are deliberately retained. Compatibility
        callers may replace session IDs, but they cannot erase continuity
        safety state by writing an older disk format.
        """
        self._ensure_mutable()
        self._sessions.clear()
        for channel_id, info in sessions.items():
            self._sessions[(int(channel_id), None)] = _copy_session_info(info)
        self._enforce_limit()
        self._changed()

    def clear(self, channel_id: int, user_id: int | None) -> bool:
        self._ensure_mutable()
        key = (int(channel_id), None if user_id is None else int(user_id))
        if self._sessions.pop(key, None) is None:
            return False
        self._changed()
        return True

    def clear_all_for_channel(self, channel_id: int) -> int:
        self._ensure_mutable()
        channel_id = int(channel_id)
        keys = [key for key in self._sessions if key[0] == channel_id]
        for key in keys:
            self._sessions.pop(key, None)
        invalidated = {
            key for key in self._invalidated_providers if key[1] == channel_id
        }
        validated = {key for key in self._validated_providers if key[1] == channel_id}
        self._invalidated_providers.difference_update(invalidated)
        self._validated_providers.difference_update(validated)
        if keys or invalidated or validated:
            self._changed()
        return len(keys)

    def clear_all(self) -> None:
        self._ensure_mutable()
        if (
            not self._sessions
            and not self._invalidated_providers
            and not self._validated_providers
        ):
            return
        self._sessions.clear()
        self._invalidated_providers.clear()
        self._validated_providers.clear()
        self._changed()

    def invalidate_provider(
        self,
        provider: ProviderName,
        channel_id: int,
        user_id: int | None,
    ) -> None:
        self._ensure_mutable()
        key: ContinuityKey = (
            provider,
            int(channel_id),
            None if user_id is None else int(user_id),
        )
        changed = key not in self._invalidated_providers
        self._invalidated_providers.add(key)
        if key in self._validated_providers:
            self._validated_providers.remove(key)
            changed = True
        if provider == "claude":
            changed = self._sessions.pop((key[1], key[2]), None) is not None or changed
        if changed:
            self._changed()

    def validate_provider(
        self,
        provider: ProviderName,
        channel_id: int,
        user_id: int | None,
    ) -> None:
        self._ensure_mutable()
        key: ContinuityKey = (
            provider,
            int(channel_id),
            None if user_id is None else int(user_id),
        )
        changed = False
        if key in self._invalidated_providers:
            self._invalidated_providers.remove(key)
            changed = True
        if (
            self._invalidate_all_provider_continuity
            and key not in self._validated_providers
        ):
            self._validated_providers.add(key)
            changed = True
        if changed:
            self._changed()

    def is_provider_invalid(
        self,
        provider: ProviderName,
        channel_id: int,
        user_id: int | None,
    ) -> bool:
        key: ContinuityKey = (
            provider,
            int(channel_id),
            None if user_id is None else int(user_id),
        )
        return key in self._invalidated_providers or (
            self._invalidate_all_provider_continuity
            and key not in self._validated_providers
        )

    def channel_projection(self) -> OrderedDict[int, SessionInfo]:
        result: OrderedDict[int, SessionInfo] = OrderedDict()
        for (channel_id, user_id), info in self._sessions.items():
            if user_id is None:
                result[channel_id] = _copy_session_info(info)
        return result

    def request_save(self) -> None:
        self._ensure_mutable()
        self._request_save()

    def _request_save(self) -> None:
        snapshot = self.snapshot()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._save_snapshot(snapshot)
            except Exception as exc:
                self._pending_snapshot = snapshot
                self._last_save_error = exc
                raise
            else:
                self._last_save_error = None
                self._pending_snapshot = None
            return
        self._pending_snapshot = snapshot
        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._drain_saves(self._epoch))
            self._save_tasks.add(self._save_task)
            self._save_task.add_done_callback(self._save_done)

    async def _drain_saves(self, epoch: int) -> None:
        while self._pending_snapshot is not None and epoch == self._epoch:
            snapshot = self._pending_snapshot
            self._pending_snapshot = None
            try:
                await asyncio.to_thread(self._save_snapshot, snapshot)
                if epoch == self._epoch:
                    self._last_save_error = None
            except asyncio.CancelledError:
                if epoch == self._epoch and self._pending_snapshot is None:
                    self._pending_snapshot = snapshot
                raise
            except Exception as exc:  # the next mutation or flush retries it
                if epoch == self._epoch:
                    self._last_save_error = exc
                    # A newer pending revision subsumes the failed snapshot.
                    # Never overwrite it with an older revision.
                    if (
                        self._pending_snapshot is None
                        or self._pending_snapshot.revision < snapshot.revision
                    ):
                        self._pending_snapshot = snapshot
                    log.error("session registry save failed: %s", exc, exc_info=True)
                return

    async def flush(self) -> None:
        await self.ensure_loaded()
        self._ensure_mutable()
        await self._flush_pending()

    async def close(self) -> None:
        """Reject new work and durably drain work accepted before close."""

        if self._close_error is not None:
            raise self._close_error
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(
                self._close_once(),
                name="session-registry-close",
            )
            self._close_task = task
            task.add_done_callback(self._close_done)
        await asyncio.shield(task)

    async def _close_once(self) -> None:
        load_task = self._load_task
        if load_task is not None:
            await load_task
        if self._load_error is not None:
            # A synchronous first-use failure has no shared load task. Keep
            # the same repository exception as the lifecycle's close failure,
            # matching the asynchronous first-use path above.
            raise self._load_error
        if self._loaded:
            await self._flush_pending()

    async def _flush_pending(self) -> None:
        if self._pending_snapshot is not None and (
            self._save_task is None or self._save_task.done()
        ):
            # The snapshot predates the close fence. Retrying it is part of
            # close, not a post-close mutation.
            self._request_save()
        task = self._save_task
        if task is not None:
            await task
        if self._pending_snapshot is not None:
            error = self._last_save_error or RuntimeError("session save did not drain")
            raise error

    def flush_sync(self) -> None:
        self._ensure_mutable()
        # Stop the current drain generation before waiting on the repository
        # lock. An already-running thread may finish its old write first, but
        # this latest snapshot is serialized after it and therefore wins.
        snapshot = self.snapshot()
        self._epoch += 1
        task = self._save_task
        if task is not None and not task.done():
            task.cancel()
        self._save_task = None
        self._save_tasks.clear()
        self._pending_snapshot = None
        try:
            self._save_snapshot(snapshot)
        except Exception as exc:
            self._pending_snapshot = snapshot
            self._last_save_error = exc
            raise
        else:
            self._last_save_error = None

    def reset_memory_only(self) -> None:
        """Clear process state without persistence, for isolated tests."""
        load_task = self._load_task
        if load_task is not None and not load_task.done():
            load_task.cancel()
        close_task = self._close_task
        if close_task is not None and not close_task.done():
            close_task.cancel()
        task = self._save_task
        if task is not None and not task.done():
            task.cancel()
        self._epoch += 1
        self._save_task = None
        self._save_tasks.clear()
        self._sessions.clear()
        self._invalidated_providers.clear()
        self._invalidate_all_provider_continuity = False
        self._validated_providers.clear()
        self._pending_snapshot = None
        self._last_save_error = None
        self._load_task = None
        self._load_owner = None
        self._load_error = None
        self._load_failure = None
        self._close_task = None
        self._close_error = None
        self._closed = False
        self._revision = 0
        self._loaded = False

    def _changed(self) -> None:
        self._ensure_mutable()
        self._loaded = True
        self._revision += 1
        self.request_save()

    def _enforce_limit(self) -> None:
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)

    def _save_done(self, task: asyncio.Task) -> None:
        self._save_tasks.discard(task)
        if self._save_task is task:
            self._save_task = None

    def _load_done(self, task: asyncio.Task[RegistrySnapshot]) -> None:
        if self._load_task is not task:
            # ``reset_memory_only`` starts a new test lifecycle. A delayed
            # callback from the prior lifecycle must not restore its failure.
            return
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if isinstance(error, Exception) and self._load_error is None:
            self._load_error = error

    def _load_owned(self, owner: object) -> RegistrySnapshot:
        try:
            return self._load(force=False, owner=owner)
        finally:
            if self._load_owner is owner:
                self._load_owner = None

    def _load_is_admitted(self, owner: object | None) -> bool:
        if owner is None:
            return not self._closed
        return self._load_owner is owner

    def _close_done(self, task: asyncio.Task[None]) -> None:
        """Retrieve close failures even when every waiter was cancelled."""

        if self._close_task is not task:
            return
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if isinstance(error, Exception):
            self._close_error = error

    def _ensure_mutable(self) -> None:
        if self._closed:
            raise SessionRegistryClosedError("session registry is closed")
        if self._load_error is not None:
            self._raise_sticky_load_error()
        if not self._loaded:
            try:
                self.load()
            except SessionRegistryClosedError:
                raise
            except Exception:
                if self._load_error is not None:
                    self._raise_sticky_load_error()
                raise
        # Loading can race close from another thread. No public writer may
        # cross either the close fence or a sticky first-use failure.
        if self._closed:
            raise SessionRegistryClosedError("session registry is closed")
        if self._load_error is not None:
            self._raise_sticky_load_error()

    def _raise_sticky_load_error(self) -> NoReturn:
        cause = self._load_error
        if cause is None:
            raise RuntimeError("session registry has no sticky load failure")
        failure = self._load_failure
        if failure is None:
            failure = SessionRegistryLoadError("session registry first-use load failed")
            failure.__cause__ = cause
            failure.__suppress_context__ = True
            self._load_failure = failure
        raise failure

    def _save_snapshot(self, snapshot: RegistrySnapshot) -> None:
        with self._repository_save_lock:
            self.repository.save(snapshot)


__all__ = [
    "ProviderName",
    "RegistrySnapshot",
    "SessionInfo",
    "SessionRecord",
    "SessionRegistry",
    "SessionRegistryClosedError",
    "SessionRegistryLoadError",
    "SessionRepository",
]

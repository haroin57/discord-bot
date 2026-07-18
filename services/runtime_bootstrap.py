"""Transactional process bootstrap and root-owned lifecycle resources."""

from __future__ import annotations

import asyncio
import atexit
import fcntl
import logging
import logging.handlers
import os
import signal
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from types import FrameType
from typing import Any

from dotenv import load_dotenv


class RuntimeConfigurationError(RuntimeError):
    """Runtime settings or filesystem ownership validation failed."""


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Validated immutable settings consumed by the composition root."""

    bot_dir: str
    state_dir: str
    history_dir: str
    sessions_path: str
    runtime_state_path: str
    invest_kb_path: str
    settings_path: str
    credentials_path: str
    attachment_dir: str
    mermaid_output_dir: str
    table_renders_dir: str
    math_renders_dir: str
    discord_token: str = field(repr=False)
    web_api_host: str
    web_api_port: int
    system_prompt: str
    session_ttl_s: int
    math_presentation_mode: str


class RuntimeShutdownError(RuntimeError):
    """One or more independent runtime cleanup operations failed."""

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures = tuple(failures)
        summary = ", ".join(f"{name}: {error}" for name, error in failures)
        super().__init__(f"runtime shutdown failed ({summary})")


class RootLifecycleError(RuntimeError):
    """One or more root-owned cleanup callbacks failed."""

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures = tuple(failures)
        summary = ", ".join(f"{name}: {error}" for name, error in failures)
        super().__init__(f"root lifecycle cleanup failed ({summary})")


@dataclass(slots=True)
class _RootCloseOutcome:
    cancellations: list[tuple[str, asyncio.CancelledError]] = field(
        default_factory=list
    )
    failures: list[tuple[str, Exception]] = field(default_factory=list)
    fatal_errors: list[tuple[str, BaseException]] = field(default_factory=list)


@dataclass(slots=True)
class _LoggingLease:
    logger: logging.Logger
    handler: logging.Handler
    previous_level: int | None
    listener: logging.handlers.QueueListener | None
    downstream: tuple[logging.Handler, ...]
    owners: dict[object, int] = field(default_factory=dict)
    external_level_override: int | None = None
    has_external_level_override: bool = False
    closed: bool = False


_LOGGING_LEASE_ATTR = "_discord_bot_lifecycle_logging_lease"
_LOGGING_LEASE_LOCK = threading.RLock()


class DiscordTokenError(RuntimeConfigurationError):
    """The immutable runtime token is absent or blank."""


class BootstrapState(Enum):
    NEW = auto()
    INITIALIZING = auto()
    READY = auto()
    FAILED = auto()
    SHUTTING_DOWN = auto()
    CLOSED = auto()


def _parse_positive_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    maximum: int | None = None,
) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeConfigurationError(f"{name} must be positive")
    if maximum is not None and value > maximum:
        raise RuntimeConfigurationError(f"{name} must be at most {maximum}")
    return value


def build_runtime_settings(
    environ: Mapping[str, str],
    *,
    bot_dir: str,
    system_prompt: str = "You are a helpful assistant running as a Discord bot.",
) -> RuntimeSettings:
    """Parse all root settings without I/O or global mutation."""
    from config.paths import build_runtime_paths

    paths = build_runtime_paths(environ, bot_dir=bot_dir)
    token = environ.get("DISCORD_TOKEN", "")
    if not isinstance(token, str) or not token.strip():
        raise DiscordTokenError(
            "DISCORD_TOKEN environment variable is required but not set "
            "(or contains only whitespace). Configure it in .env or the "
            "process environment before starting the bot."
        )
    host = environ.get("WEB_API_HOST", "127.0.0.1")
    if (
        not isinstance(host, str)
        or not host.strip()
        or any(ch.isspace() for ch in host)
    ):
        raise RuntimeConfigurationError("WEB_API_HOST must be a non-empty host")
    port = _parse_positive_int(environ, "WEB_API_PORT", 3002, maximum=65535)
    raw_mode = environ.get("MATH_PRESENTATION_MODE", "legacy")
    if not isinstance(raw_mode, str):
        raise RuntimeConfigurationError("MATH_PRESENTATION_MODE must be a string")
    mode = raw_mode.strip().lower()
    if mode not in {"legacy", "adaptive"}:
        raise RuntimeConfigurationError(
            "MATH_PRESENTATION_MODE must be either 'legacy' or 'adaptive'"
        )
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise RuntimeConfigurationError("system_prompt must be a non-empty string")
    return RuntimeSettings(
        bot_dir=paths.bot_dir,
        state_dir=paths.state_dir,
        history_dir=paths.history_dir,
        sessions_path=paths.sessions_path,
        runtime_state_path=paths.runtime_state_path,
        invest_kb_path=paths.invest_kb_path,
        settings_path=paths.settings_path,
        credentials_path=paths.credentials_path,
        attachment_dir=paths.attachment_dir,
        mermaid_output_dir=paths.mermaid_output_dir,
        table_renders_dir=paths.table_renders_dir,
        math_renders_dir=paths.math_renders_dir,
        discord_token=token,
        web_api_host=host,
        web_api_port=port,
        system_prompt=system_prompt,
        session_ttl_s=paths.session_ttl_s,
        math_presentation_mode=mode,
    )


def _approved_root(path: str, roots: tuple[str, ...]) -> str:
    normalised = os.path.abspath(path)
    candidates = []
    for root in roots:
        root = os.path.abspath(root)
        try:
            if os.path.commonpath((root, normalised)) == root and normalised != root:
                candidates.append(root)
        except ValueError:
            continue
    if not candidates:
        raise RuntimeConfigurationError(f"path is outside approved roots: {path!r}")
    return max(candidates, key=len)


def _open_directory_nofollow(path: str, *, roots: tuple[str, ...]) -> int:
    """Open a directory by walking below a trusted anchor without symlinks."""
    root = _approved_root(path, roots)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current_fd = os.open(root, flags)
    try:
        relative = os.path.relpath(os.path.abspath(path), root)
        for component in relative.split(os.sep):
            if component in {"", ".", ".."}:
                raise RuntimeConfigurationError(f"invalid path component in {path!r}")
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def ensure_private_directory(
    path: str,
    *,
    roots: tuple[str, ...] | None = None,
) -> None:
    """Create and verify an owned 0700 directory without following symlinks."""
    if not isinstance(path, str) or not path.strip() or not os.path.isabs(path):
        raise RuntimeConfigurationError(
            "runtime directory must be a non-empty absolute path"
        )
    approved = roots or (
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        os.path.abspath(tempfile.gettempdir()),
        "/var/lib",
        "/var/cache",
    )
    root = _approved_root(path, approved)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current_fd = os.open(root, flags)
    try:
        relative = os.path.relpath(os.path.abspath(path), root)
        for component in relative.split(os.sep):
            if component in {"", ".", ".."}:
                raise RuntimeConfigurationError(f"invalid path component in {path!r}")
            try:
                os.mkdir(component, mode=0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, flags, dir_fd=current_fd)
            try:
                info = os.fstat(next_fd)
                if not stat.S_ISDIR(info.st_mode):
                    raise RuntimeConfigurationError(
                        f"runtime path is not a directory: {path}"
                    )
                if info.st_uid != os.geteuid():
                    raise RuntimeConfigurationError(
                        f"runtime directory has the wrong owner: {path}"
                    )
                os.fchmod(next_fd, 0o700)
                if stat.S_IMODE(os.fstat(next_fd).st_mode) != 0o700:
                    raise RuntimeConfigurationError(
                        f"runtime directory mode is not 0700: {path}"
                    )
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
    except OSError as exc:
        raise RuntimeConfigurationError(
            f"secure runtime directory validation failed for {path!r}: {exc}"
        ) from exc
    finally:
        os.close(current_fd)


def _validate_private_file(path: str, *, parent: str) -> None:
    """Fail closed if an existing state file is not an owned regular 0600 file."""
    parent_fd = _open_directory_nofollow(
        path=parent,
        roots=(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            os.path.abspath(tempfile.gettempdir()),
            "/var/lib",
        ),
    )
    try:
        try:
            info = os.stat(
                os.path.basename(path), dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeConfigurationError(
                f"state file has unsafe type or owner: {path}"
            )
        if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeConfigurationError(
                f"state file has unsafe link count or mode: {path}"
            )
    finally:
        os.close(parent_fd)


class PidFileLock:
    """An inode-pinned flock whose release cannot unlink a replacement path."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._fd: int | None = None
        self._dir_fd: int | None = None
        self._name = os.path.basename(self.path)
        self._identity: tuple[int, int] | None = None
        self._owner_pid: int | None = None
        self._closed = False

    def fileno(self) -> int:
        if self._fd is None:
            raise ValueError("PID lock is not acquired")
        return self._fd

    def acquire(self) -> None:
        if self._fd is not None:
            if self._owner_pid == os.getpid():
                return
            # A fork child inherited the parent's open-file description. Drop
            # only the child's descriptor copies before attempting a new lock;
            # explicit LOCK_UN would release the parent's shared flock.
            self._close_descriptors(unlock=False)
            self._closed = False
        parent = os.path.dirname(self.path)
        dir_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(self._name, flags, 0o600, dir_fd=dir_fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise RuntimeConfigurationError(
                    "PID file has unsafe type, owner, or links"
                )
            os.fchmod(fd, 0o600)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise RuntimeConfigurationError("PID file mode is not 0600")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current = os.stat(self._name, dir_fd=dir_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeConfigurationError(
                    "PID file path changed during acquisition"
                )
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
        except BaseException:
            if "fd" in locals():
                os.close(fd)
            os.close(dir_fd)
            raise
        self._fd = fd
        self._dir_fd = dir_fd
        self._identity = (info.st_dev, info.st_ino)
        self._owner_pid = os.getpid()

    def read_pid(self) -> str:
        fd = self.fileno()
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 64).decode("ascii", errors="replace").strip()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fd, dir_fd = self._fd, self._dir_fd
        owner_pid = self._owner_pid
        self._fd = None
        self._dir_fd = None
        self._owner_pid = None
        if fd is None or dir_fd is None:
            return
        if owner_pid != os.getpid():
            # Never unlink or unlock the parent's PID lock from a fork child.
            os.close(fd)
            os.close(dir_fd)
            return
        try:
            try:
                current = os.stat(self._name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None and self._identity == (
                current.st_dev,
                current.st_ino,
            ):
                os.unlink(self._name, dir_fd=dir_fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                os.close(dir_fd)

    def _close_descriptors(self, *, unlock: bool) -> None:
        fd, dir_fd = self._fd, self._dir_fd
        self._fd = None
        self._dir_fd = None
        self._identity = None
        self._owner_pid = None
        if fd is None or dir_fd is None:
            return
        try:
            if unlock:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            os.close(dir_fd)


class RootLifecycle:
    """Own process-root resources and release them once in reverse order."""

    def __init__(self) -> None:
        self._callbacks: list[tuple[str, Callable[[], None]]] = []
        self._closed = False
        self._closing = False
        self._closing_thread_id: int | None = None
        self._lock = threading.RLock()
        self._close_condition = threading.Condition(self._lock)
        self._close_task: asyncio.Task[_RootCloseOutcome] | None = None
        self._atexit_registered = False
        self._owned_logging_handlers: set[int] = set()
        self._logging_owner_token = object()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def callback(self, name: str, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("RootLifecycle is closing or already closed")
            self._callbacks.append((name, callback))

    def arm_atexit(self) -> None:
        with self._lock:
            if self._closed or self._closing or self._atexit_registered:
                return
            atexit.register(self.close)
            self._atexit_registered = True

    def add_signal_handler(
        self,
        loop: asyncio.AbstractEventLoop,
        sig: signal.Signals,
        callback: Callable[..., None],
        *args: object,
    ) -> bool:
        with self._lock:
            if self._closed or self._closing:
                return False
            previous: int | Callable[[int, FrameType | None], Any] | None = (
                signal.getsignal(sig)
            )
            try:
                loop.add_signal_handler(sig, callback, *args)
            except (NotImplementedError, RuntimeError):
                return False

            def _restore() -> None:
                removal_error: Exception | None = None
                try:
                    loop.remove_signal_handler(sig)
                except Exception as exc:
                    removal_error = exc
                signal.signal(sig, previous)
                if removal_error is not None:
                    raise removal_error

            # RLock permits a hostile/re-entrant loop implementation to close
            # the lifecycle during add_signal_handler. Roll back immediately
            # instead of leaving an unowned process signal handler installed.
            if self._closed:
                _restore()
                return False
            self._callbacks.append((f"signal:{sig.name}", _restore))
            return True

    def own_logging(
        self,
        *,
        logger: logging.Logger,
        handler: logging.Handler,
        listener: logging.handlers.QueueListener | None = None,
        downstream: tuple[logging.Handler, ...] = (),
        previous_level: int | None = None,
    ) -> None:
        configured_level = logger.level
        if previous_level is None:
            previous_level = getattr(handler, "_discord_bot_previous_level", None)

        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("RootLifecycle is closing or already closed")
            setattr(handler, "_discord_bot_configured_level", configured_level)
            identity = id(handler)
            with _LOGGING_LEASE_LOCK:
                lease = getattr(handler, _LOGGING_LEASE_ATTR, None)
                if lease is None:
                    lease = _LoggingLease(
                        logger=logger,
                        handler=handler,
                        previous_level=previous_level,
                        listener=listener,
                        downstream=downstream,
                    )
                    setattr(handler, _LOGGING_LEASE_ATTR, lease)
                elif not isinstance(lease, _LoggingLease) or lease.closed:
                    raise RuntimeError("logging handler lease is unavailable")
                elif lease.logger is not logger:
                    raise RuntimeError(
                        "logging handler lease belongs to another logger"
                    )
                else:
                    if lease.listener is None and listener is not None:
                        lease.listener = listener
                    if not lease.downstream and downstream:
                        lease.downstream = downstream
                lease.owners.pop(self._logging_owner_token, None)
                lease.owners[self._logging_owner_token] = configured_level
            if identity not in self._owned_logging_handlers:
                self._owned_logging_handlers.add(identity)
                self._callbacks.append(
                    (
                        "logging",
                        lambda: self._release_logging_lease(lease),
                    )
                )

    def _release_logging_lease(self, lease: _LoggingLease) -> None:
        failures: list[Exception] = []
        cancellations: list[asyncio.CancelledError] = []
        fatal_errors: list[BaseException] = []

        def attempt(operation: Callable[[], None]) -> None:
            try:
                operation()
            except asyncio.CancelledError as exc:
                cancellations.append(exc)
            except Exception as exc:
                failures.append(exc)
            except BaseException as exc:
                fatal_errors.append(exc)

        final_owner = False
        with _LOGGING_LEASE_LOCK:
            if lease.closed or self._logging_owner_token not in lease.owners:
                return
            active_level = next(reversed(lease.owners.values()))
            if lease.logger.level != active_level:
                lease.external_level_override = lease.logger.level
                lease.has_external_level_override = True
            del lease.owners[self._logging_owner_token]
            if lease.owners:
                remaining_level = next(reversed(lease.owners.values()))
                setattr(
                    lease.handler,
                    "_discord_bot_configured_level",
                    remaining_level,
                )
                if lease.has_external_level_override:
                    external_level = lease.external_level_override
                    if external_level is not None:
                        restore_external_level: int = external_level
                        attempt(lambda: lease.logger.setLevel(restore_external_level))
                elif lease.logger.level == active_level:
                    attempt(lambda: lease.logger.setLevel(remaining_level))
            else:
                lease.closed = True
                final_owner = True

        if not final_owner:
            self._raise_logging_cleanup_errors(
                cancellations,
                fatal_errors,
                failures,
            )
            return

        def stop_listener() -> None:
            if (
                lease.listener is not None
                and getattr(lease.listener, "_thread", None) is not None
            ):
                lease.listener.stop()

        attempt(stop_listener)
        attempt(lambda: lease.logger.removeHandler(lease.handler))
        attempt(lease.handler.close)
        for child in lease.downstream:
            attempt(child.close)
        if lease.has_external_level_override:
            external_level = lease.external_level_override
            if external_level is not None and lease.logger.level != external_level:
                restore_external_level = external_level
                attempt(lambda: lease.logger.setLevel(restore_external_level))
        elif lease.previous_level is not None and lease.logger.level == active_level:
            restore_level: int = lease.previous_level
            attempt(lambda: lease.logger.setLevel(restore_level))
        self._raise_logging_cleanup_errors(
            cancellations,
            fatal_errors,
            failures,
        )

    @staticmethod
    def _raise_logging_cleanup_errors(
        cancellations: list[asyncio.CancelledError],
        fatal_errors: list[BaseException],
        failures: list[Exception],
    ) -> None:
        if fatal_errors:
            fatal = fatal_errors[0]
            details = [
                *(str(other) for other in fatal_errors[1:]),
                *(str(cancel) for cancel in cancellations),
                *(str(failure) for failure in failures),
            ]
            if details:
                fatal.add_note("logging cleanup failures: " + "; ".join(details))
            raise fatal
        if cancellations:
            cancel = cancellations[0]
            details = [
                *(str(other) for other in cancellations[1:]),
                *(str(failure) for failure in failures),
            ]
            if details:
                cancel.add_note("logging cleanup failures: " + "; ".join(details))
            raise cancel
        if failures:
            raise RuntimeError(
                "; ".join(str(failure) for failure in failures)
            ) from failures[0]

    def _drain_callbacks(self) -> _RootCloseOutcome:
        outcome = _RootCloseOutcome()
        while True:
            with self._lock:
                if not self._callbacks:
                    break
                name, callback = self._callbacks.pop()
            try:
                callback()
            except asyncio.CancelledError as exc:
                outcome.cancellations.append((name, exc))
            except Exception as exc:
                outcome.failures.append((name, exc))
            except BaseException as exc:
                outcome.fatal_errors.append((name, exc))

        with self._lock:
            unregister_atexit = self._atexit_registered
            self._atexit_registered = False
        if unregister_atexit:
            try:
                atexit.unregister(self.close)
            except asyncio.CancelledError as exc:
                outcome.cancellations.append(("atexit", exc))
            except Exception as exc:
                outcome.failures.append(("atexit", exc))
            except BaseException as exc:
                outcome.fatal_errors.append(("atexit", exc))

        with self._close_condition:
            self._closed = True
            self._closing = False
            self._closing_thread_id = None
            self._close_condition.notify_all()
        return outcome

    @staticmethod
    def _raise_close_outcome(
        outcome: _RootCloseOutcome,
        outer_cancel: asyncio.CancelledError | None = None,
    ) -> None:
        if outer_cancel is not None:
            other_cancellations = [
                f"{name}: {error}"
                for name, error in outcome.cancellations
                if error is not outer_cancel
            ]
            details = [
                *other_cancellations,
                *(f"{name}: {error}" for name, error in outcome.fatal_errors),
                *(f"{name}: {error}" for name, error in outcome.failures),
            ]
            if details:
                outer_cancel.add_note(
                    "root lifecycle cleanup failures: " + ", ".join(details)
                )
            if outcome.fatal_errors and outer_cancel.__context__ is None:
                outer_cancel.__context__ = outcome.fatal_errors[0][1]
            raise outer_cancel
        if outcome.fatal_errors:
            fatal = outcome.fatal_errors[0][1]
            remaining = [
                *(f"{name}: {error}" for name, error in outcome.fatal_errors[1:]),
                *(f"{name}: {error}" for name, error in outcome.cancellations),
                *(f"{name}: {error}" for name, error in outcome.failures),
            ]
            if remaining:
                fatal.add_note(
                    "root lifecycle cleanup failures: " + ", ".join(remaining)
                )
            raise fatal
        if outcome.cancellations:
            first_cancel = outcome.cancellations[0][1]
            details = [
                f"{name}: {error}" for name, error in outcome.cancellations[1:]
            ] + [f"{name}: {error}" for name, error in outcome.failures]
            if details:
                first_cancel.add_note(
                    "root lifecycle cleanup failures: " + ", ".join(details)
                )
            raise first_cancel
        if outcome.failures:
            raise RootLifecycleError(outcome.failures)

    async def _run_close_task(self) -> _RootCloseOutcome:
        # Give the caller a cancellation point after ownership admission. The
        # lifecycle retains this task and shields it from every outer cancel.
        await asyncio.sleep(0)
        return self._drain_callbacks()

    async def aclose(self) -> None:
        """Close every resource despite repeated cancellation of the caller."""
        with self._close_condition:
            if self._closed:
                return
            task = self._close_task
            if task is None:
                if self._closing:
                    raise RuntimeError("synchronous lifecycle close is in progress")
                self._closing = True
                self._closing_thread_id = threading.get_ident()
                try:
                    task = asyncio.create_task(
                        self._run_close_task(),
                        name="root-lifecycle-cleanup",
                    )
                except BaseException:
                    self._closing = False
                    self._closing_thread_id = None
                    self._close_condition.notify_all()
                    raise
                self._close_task = task

        first_cancel: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if first_cancel is None:
                    first_cancel = exc
        try:
            outcome = task.result()
        except asyncio.CancelledError as worker_cancel:
            with self._close_condition:
                if not self._closed:
                    self._closing = False
                    self._closing_thread_id = None
                    self._close_task = None
                    self._close_condition.notify_all()
            raise first_cancel or worker_cancel
        self._raise_close_outcome(outcome, first_cancel)

    def close(self) -> None:
        """Synchronously release every callback for atexit and sync roots."""
        thread_id = threading.get_ident()
        with self._close_condition:
            if self._closed:
                return
            if self._closing:
                if self._closing_thread_id == thread_id:
                    return
                while self._closing and not self._closed:
                    self._close_condition.wait()
                return
            self._closing = True
            self._closing_thread_id = thread_id
        outcome = self._drain_callbacks()
        self._raise_close_outcome(outcome)


class RuntimeBootstrap:
    """Perform runtime I/O as a rollback-safe, instance-owned transaction."""

    _CONFIG_NAMES = (
        "DISCORD_TOKEN",
        "BOT_DIR",
        "STATE_DIR",
        "HISTORY_DIR",
        "SESSIONS_PATH",
        "RUNTIME_STATE_PATH",
        "INVEST_KB_PATH",
        "SETTINGS_PATH",
        "CREDENTIALS_PATH",
        "ATTACHMENT_DIR",
        "MERMAID_OUTPUT_DIR",
        "TABLE_RENDERS_DIR",
        "MATH_RENDERS_DIR",
        "SESSION_TTL_S",
        "WEB_API_HOST",
        "WEB_API_PORT",
        "MATH_PRESENTATION_MODE",
        "ALLOWED_IMAGE_DIRS",
        "SYSTEM_PROMPT",
        "RESTRICTED_SYSTEM_PROMPT",
    )

    def __init__(
        self, *, bot_dir: str | None = None, dotenv_path: str | None = None
    ) -> None:
        self._bot_dir = os.path.abspath(
            bot_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self._dotenv_path = dotenv_path or os.path.join(self._bot_dir, ".env")
        self._environment_loaded = False
        self._settings: RuntimeSettings | None = None
        self._state = BootstrapState.NEW
        self._state_lock = threading.RLock()
        self._state_condition = threading.Condition(self._state_lock)
        self._shutdown_requested = False
        self._shutdown_claim_available = False
        self._config_snapshot: dict[str, object] | None = None
        self._installed_config: dict[str, object] | None = None
        self._path_snapshot: Any | None = None

    @property
    def state(self) -> BootstrapState:
        with self._state_lock:
            return self._state

    def load_environment(self) -> None:
        with self._state_lock:
            if self._environment_loaded:
                return
            load_dotenv(self._dotenv_path, override=False)
            self._environment_loaded = True

    def initialize(self) -> RuntimeSettings:
        with self._state_condition:
            if self._state is BootstrapState.READY and self._settings is not None:
                return self._settings
            if self._state is not BootstrapState.NEW:
                raise RuntimeError(
                    f"RuntimeBootstrap cannot initialize from {self._state.name}"
                )
            if self._shutdown_requested:
                raise RuntimeError(
                    "RuntimeBootstrap shutdown has already been requested"
                )
            self._state = BootstrapState.INITIALIZING

        try:
            self.load_environment()
            settings = build_runtime_settings(os.environ, bot_dir=self._bot_dir)
            roots = (
                settings.bot_dir,
                os.path.abspath(tempfile.gettempdir()),
                "/var/lib",
                "/var/cache",
            )
            for path in (
                settings.state_dir,
                settings.history_dir,
                settings.attachment_dir,
                settings.mermaid_output_dir,
                settings.table_renders_dir,
                settings.math_renders_dir,
            ):
                ensure_private_directory(path, roots=roots)
            _validate_private_file(settings.sessions_path, parent=settings.state_dir)
            _validate_private_file(
                settings.runtime_state_path, parent=settings.state_dir
            )

            import config

            prompt = config.load_system_prompt(bot_dir=settings.bot_dir)
            if settings.math_presentation_mode == "adaptive":
                prompt = prompt.rstrip() + "\n\n" + config.MATH_PRESENTATION_GUIDANCE
            settings = replace(settings, system_prompt=prompt)
            self._install_config(config, settings)

            from services.session import initialize_session_registry

            initialize_session_registry()
        except BaseException:
            self._rollback_config()
            with self._state_condition:
                self._settings = None
                self._state = BootstrapState.FAILED
                self._state_condition.notify_all()
            raise

        with self._state_condition:
            self._settings = settings
            if self._shutdown_requested:
                self._state = BootstrapState.SHUTTING_DOWN
                self._shutdown_claim_available = True
                interrupted = True
            else:
                self._state = BootstrapState.READY
                interrupted = False
            self._state_condition.notify_all()
        if interrupted:
            raise RuntimeError(
                "RuntimeBootstrap shutdown was requested during initialization"
            )
        return settings

    def _install_config(self, config: Any, settings: RuntimeSettings) -> None:
        import config.paths as config_paths

        self._config_snapshot = {
            name: getattr(config, name) for name in self._CONFIG_NAMES
        }
        self._path_snapshot = config_paths.RuntimePaths(
            bot_dir=config_paths.BOT_DIR,
            state_dir=config_paths.STATE_DIR,
            history_dir=config_paths.HISTORY_DIR,
            sessions_path=config_paths.SESSIONS_PATH,
            runtime_state_path=config_paths.RUNTIME_STATE_PATH,
            invest_kb_path=config_paths.INVEST_KB_PATH,
            settings_path=config_paths.SETTINGS_PATH,
            credentials_path=config_paths.CREDENTIALS_PATH,
            attachment_dir=config_paths.ATTACHMENT_DIR,
            mermaid_output_dir=config_paths.MERMAID_OUTPUT_DIR,
            table_renders_dir=config_paths.TABLE_RENDERS_DIR,
            math_renders_dir=config_paths.MATH_RENDERS_DIR,
            session_ttl_s=config_paths.SESSION_TTL_S,
        )
        paths = config_paths.RuntimePaths(
            bot_dir=settings.bot_dir,
            state_dir=settings.state_dir,
            history_dir=settings.history_dir,
            sessions_path=settings.sessions_path,
            runtime_state_path=settings.runtime_state_path,
            invest_kb_path=settings.invest_kb_path,
            settings_path=settings.settings_path,
            credentials_path=settings.credentials_path,
            attachment_dir=settings.attachment_dir,
            mermaid_output_dir=settings.mermaid_output_dir,
            table_renders_dir=settings.table_renders_dir,
            math_renders_dir=settings.math_renders_dir,
            session_ttl_s=settings.session_ttl_s,
        )
        config_paths.install_runtime_paths(paths)
        restricted_prompt = config.RESTRICTED_SYSTEM_PROMPT
        guidance_suffix = "\n\n" + config.MATH_PRESENTATION_GUIDANCE
        if restricted_prompt.endswith(guidance_suffix):
            restricted_prompt = restricted_prompt[: -len(guidance_suffix)]
        if settings.math_presentation_mode == "adaptive":
            restricted_prompt += guidance_suffix
        installed = {
            "DISCORD_TOKEN": settings.discord_token,
            "BOT_DIR": settings.bot_dir,
            "STATE_DIR": settings.state_dir,
            "HISTORY_DIR": settings.history_dir,
            "SESSIONS_PATH": settings.sessions_path,
            "RUNTIME_STATE_PATH": settings.runtime_state_path,
            "INVEST_KB_PATH": settings.invest_kb_path,
            "SETTINGS_PATH": settings.settings_path,
            "CREDENTIALS_PATH": settings.credentials_path,
            "ATTACHMENT_DIR": settings.attachment_dir,
            "MERMAID_OUTPUT_DIR": settings.mermaid_output_dir,
            "TABLE_RENDERS_DIR": settings.table_renders_dir,
            "MATH_RENDERS_DIR": settings.math_renders_dir,
            "SESSION_TTL_S": settings.session_ttl_s,
            "WEB_API_HOST": settings.web_api_host,
            "WEB_API_PORT": settings.web_api_port,
            "MATH_PRESENTATION_MODE": settings.math_presentation_mode,
            "ALLOWED_IMAGE_DIRS": (
                settings.attachment_dir,
                settings.mermaid_output_dir,
                os.path.join(tempfile.gettempdir(), "discord_bot_attachments"),
                settings.table_renders_dir,
                settings.math_renders_dir,
            ),
            "SYSTEM_PROMPT": settings.system_prompt,
            "RESTRICTED_SYSTEM_PROMPT": restricted_prompt,
        }
        for name, value in installed.items():
            setattr(config, name, value)
        config._install_runtime_system_prompt(settings.system_prompt)
        self._installed_config = installed

    def _rollback_config(self) -> None:
        snapshot = self._config_snapshot
        installed = self._installed_config
        path_snapshot = self._path_snapshot
        self._config_snapshot = None
        self._installed_config = None
        self._path_snapshot = None
        if snapshot is None:
            return
        import config
        import config.paths as config_paths

        for name, old_value in snapshot.items():
            if installed is None or getattr(config, name, object()) == installed.get(
                name
            ):
                setattr(config, name, old_value)
        if path_snapshot is not None:
            config_paths.install_runtime_paths(path_snapshot)

    def restore_runtime_state(self) -> None:
        if self._state is not BootstrapState.READY:
            raise RuntimeError("runtime state cannot be restored before initialization")
        import services.runtime_state as runtime_state

        runtime_state.load_all()

    def _claim_shutdown(self) -> bool:
        with self._state_condition:
            self._shutdown_requested = True
            while True:
                if self._state is BootstrapState.INITIALIZING:
                    self._state_condition.wait()
                    continue
                if self._state is BootstrapState.SHUTTING_DOWN:
                    if self._shutdown_claim_available:
                        self._shutdown_claim_available = False
                        return True
                    self._state_condition.wait()
                    continue
                if self._state in {
                    BootstrapState.NEW,
                    BootstrapState.FAILED,
                    BootstrapState.CLOSED,
                }:
                    self._rollback_config()
                    self._state = BootstrapState.CLOSED
                    self._state_condition.notify_all()
                    return False
                if self._state is BootstrapState.READY:
                    self._state = BootstrapState.SHUTTING_DOWN
                    return True
                return False

    def _finish_shutdown(self) -> None:
        self._rollback_config()
        with self._state_condition:
            self._settings = None
            self._shutdown_claim_available = False
            self._state = BootstrapState.CLOSED
            self._state_condition.notify_all()

    async def shutdown(self) -> None:
        first_cancel: asyncio.CancelledError | None = None
        claim_task = asyncio.create_task(asyncio.to_thread(self._claim_shutdown))
        while not claim_task.done():
            try:
                await asyncio.shield(claim_task)
            except asyncio.CancelledError as exc:
                first_cancel = first_cancel or exc
        claimed = claim_task.result()
        if not claimed:
            if first_cancel is not None:
                raise first_cancel
            return
        failures: list[tuple[str, Exception]] = []
        try:
            try:
                from services.claude_api import kill_orphan_claude_cli

                kill_orphan_claude_cli()
            except asyncio.CancelledError as exc:
                first_cancel = first_cancel or exc
            except Exception as exc:
                failures.append(("claude", exc))
            try:
                from services.codex_app_server.manager import close_all_codex_sessions

                await close_all_codex_sessions()
            except asyncio.CancelledError as exc:
                first_cancel = first_cancel or exc
            except Exception as exc:
                failures.append(("codex", exc))
            try:
                from services.session import flush_session_registry

                await flush_session_registry()
            except asyncio.CancelledError as exc:
                first_cancel = first_cancel or exc
            except Exception as exc:
                failures.append(("session_registry", exc))
        finally:
            self._finish_shutdown()
        if first_cancel is not None:
            if failures:
                first_cancel.add_note(
                    "runtime shutdown failures: "
                    + ", ".join(f"{name}: {error}" for name, error in failures)
                )
            raise first_cancel
        if failures:
            raise RuntimeShutdownError(failures)

    def shutdown_sync(self) -> None:
        if not self._claim_shutdown():
            return
        failures: list[tuple[str, Exception]] = []
        try:
            try:
                from services.claude_api import kill_orphan_claude_cli

                kill_orphan_claude_cli()
            except Exception as exc:
                failures.append(("claude", exc))
            try:
                from services.codex_app_server.manager import close_all_codex_sessions

                asyncio.run(close_all_codex_sessions())
            except Exception as exc:
                failures.append(("codex", exc))
            try:
                from services.session import session_registry

                session_registry().flush_sync()
            except Exception as exc:
                failures.append(("session_registry", exc))
        finally:
            self._finish_shutdown()
        if failures:
            raise RuntimeShutdownError(failures)


__all__ = [
    "BootstrapState",
    "DiscordTokenError",
    "PidFileLock",
    "RootLifecycle",
    "RootLifecycleError",
    "RuntimeBootstrap",
    "RuntimeConfigurationError",
    "RuntimeSettings",
    "RuntimeShutdownError",
    "build_runtime_settings",
    "ensure_private_directory",
]

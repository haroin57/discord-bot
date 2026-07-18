"""Shared subprocess policy for CLI calls and streaming MCP transports."""

from __future__ import annotations

import asyncio
import os
import signal
import weakref
from collections.abc import Awaitable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

_T = TypeVar("_T")
_PROCESS_GROUP_POLL_INTERVAL_S = 0.02

INHERITED_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "COLORTERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "NODE_EXTRA_CA_CERTS",
        "RUST_BACKTRACE",
        "RUST_LOG",
        "CODEX_HOME",
    }
)


class SubprocessDeadlineExceeded(RuntimeError):
    pass


class ProcessOwnershipError(RuntimeError):
    """A process handle no longer proves ownership of its original group."""


class ProcessCleanupPermissionError(ProcessOwnershipError):
    """The owned process group could not be inspected or signalled."""


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    returncode: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class ProcessGroupFence:
    """Spawn-time identity used to reject PID/PGID reuse before signalling."""

    pid: int
    pgid: int | None
    generation: str | None
    session_id: int | None = None


@dataclass(frozen=True, slots=True)
class _ProcfsProcessIdentity:
    """One procfs identity snapshot used only for ownership fencing."""

    pid: int
    pgid: int
    session_id: int
    generation: str


_PROCESS_GROUP_FENCES: weakref.WeakKeyDictionary[
    asyncio.subprocess.Process,
    ProcessGroupFence,
] = weakref.WeakKeyDictionary()
_PROCFS_ROOT = Path("/proc")


def _read_process_identity(pid: int) -> _ProcfsProcessIdentity | None:
    """Read PID generation and group identity without inspecting argv or env."""
    if os.name != "posix":
        return None
    try:
        stat_text = (_PROCFS_ROOT / str(pid) / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except PermissionError as exc:
        raise ProcessCleanupPermissionError(
            f"cannot inspect procfs identity for pid {pid}"
        ) from exc
    except OSError as exc:
        raise ProcessOwnershipError(
            f"cannot read procfs identity for pid {pid}"
        ) from exc
    try:
        after_comm = stat_text.rsplit(")", 1)[1].split()
        return _ProcfsProcessIdentity(
            pid=pid,
            pgid=int(after_comm[2]),
            session_id=int(after_comm[3]),
            generation=after_comm[19],
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise ProcessOwnershipError(f"malformed procfs identity for pid {pid}") from exc


def _list_process_identities() -> tuple[_ProcfsProcessIdentity, ...]:
    """Snapshot procfs identities for group membership proof only."""
    if os.name != "posix":
        return ()
    try:
        entries = tuple(_PROCFS_ROOT.iterdir())
    except PermissionError as exc:
        raise ProcessCleanupPermissionError("cannot enumerate procfs") from exc
    except OSError as exc:
        raise ProcessOwnershipError("cannot enumerate procfs") from exc

    identities: list[_ProcfsProcessIdentity] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        identity = _read_process_identity(int(entry.name))
        if identity is not None:
            identities.append(identity)
    return tuple(identities)


def _attach_process_group_fence(
    process: asyncio.subprocess.Process,
) -> ProcessGroupFence:
    if not isinstance(process, asyncio.subprocess.Process):
        raise ProcessOwnershipError("spawn returned a non-asyncio Process handle")
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        raise ProcessOwnershipError("spawned process has no integer pid")
    pgid: int | None = None
    session_id: int | None = None
    generation: str | None = None
    if os.name == "posix":
        identity = _read_process_identity(pid)
        if identity is None:
            raise ProcessOwnershipError(
                f"process {pid} exited before ownership fencing"
            )
        pgid = identity.pgid
        session_id = identity.session_id
        generation = identity.generation
        if pgid != pid:
            raise ProcessOwnershipError(
                f"process {pid} did not start in its own process group ({pgid})"
            )
        if session_id != pid:
            raise ProcessOwnershipError(
                f"process {pid} did not start in its own session ({session_id})"
            )
    fence = ProcessGroupFence(
        pid=pid,
        pgid=pgid,
        generation=generation,
        session_id=session_id,
    )
    _PROCESS_GROUP_FENCES[process] = fence
    return fence


def get_process_group_fence(
    process: asyncio.subprocess.Process,
) -> ProcessGroupFence | None:
    if not isinstance(process, asyncio.subprocess.Process):
        return None
    fence = _PROCESS_GROUP_FENCES.get(process)
    return fence if isinstance(fence, ProcessGroupFence) else None


def build_allowlisted_env(
    overrides: Mapping[str, str] | None = None,
    *,
    source: Mapping[str, str] | None = None,
    remove: Sequence[str] = (),
) -> dict[str, str]:
    ambient = os.environ if source is None else source
    env = {
        key: value
        for key, value in ambient.items()
        if key in INHERITED_ENV_ALLOWLIST and not value.startswith("()")
    }
    if overrides:
        env.update({str(key): str(value) for key, value in overrides.items()})
    for key in remove:
        env.pop(key, None)
    return env


def deadline_after(timeout_s: float) -> float:
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    return asyncio.get_running_loop().time() + timeout_s


async def await_before_deadline(
    awaitable: Awaitable[_T],
    deadline: float,
) -> _T:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError
    return await asyncio.wait_for(awaitable, timeout=remaining)


async def spawn_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    stream_limit: int | None = None,
) -> asyncio.subprocess.Process:
    if not argv:
        raise ValueError("argv must not be empty")
    if stream_limit is not None and stream_limit <= 0:
        raise ValueError("stream_limit must be positive")
    stream_kwargs = {"limit": stream_limit} if stream_limit is not None else {}
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
        **stream_kwargs,
    )
    try:
        _attach_process_group_fence(process)
    except BaseException:
        # A failed generation fence cannot authorize even a cleanup signal.
        # Reap only a process that has already exited; otherwise fail closed.
        if process.returncode is not None:
            await process.wait()
        raise
    return process


async def await_cleanup_task(
    cleanup: Coroutine[Any, Any, _T],
    *,
    name: str,
    original_cancel: asyncio.CancelledError | None = None,
) -> _T:
    """Run cleanup to completion despite repeated cancellation.

    ``asyncio.shield`` alone only protects the inner task from one cancellation;
    a later cancellation stops the caller from awaiting that task.  Keep a
    strong reference, record every outer cancellation, retrieve the cleanup
    result, then re-raise the first cancellation that initiated teardown.
    """
    cleanup_task = asyncio.create_task(cleanup, name=name)
    cancellations: list[asyncio.CancelledError] = []
    if original_cancel is not None:
        cancellations.append(original_cancel)

    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            if cleanup_task.cancelled():
                break
            cancellations.append(exc)
        except BaseException:
            if cleanup_task.done():
                break
            raise

    try:
        result = cleanup_task.result()
    except BaseException as cleanup_exc:
        if cancellations:
            raise cancellations[0] from cleanup_exc
        raise
    if cancellations:
        raise cancellations[0]
    return result


def _owned_process_group_members(
    process: asyncio.subprocess.Process,
    fence: ProcessGroupFence,
) -> tuple[_ProcfsProcessIdentity, ...]:
    """Return members only while the spawn generation/session proof holds."""
    if not isinstance(process, asyncio.subprocess.Process):
        raise ProcessOwnershipError("unregistered non-asyncio Process handle")
    if (
        os.name != "posix"
        or fence.pgid is None
        or fence.session_id is None
        or fence.generation is None
    ):
        raise ProcessOwnershipError("process group has no complete procfs fence")
    if getattr(process, "pid", None) != fence.pid:
        raise ProcessOwnershipError("process handle pid changed after registration")
    if fence.pgid != fence.pid or fence.session_id != fence.pid:
        raise ProcessOwnershipError("spawn fence is not a private process session")

    identities = _list_process_identities()
    current_leader = next(
        (identity for identity in identities if identity.pid == fence.pid),
        None,
    )
    if current_leader is not None:
        if current_leader.generation != fence.generation:
            raise ProcessOwnershipError(
                f"pid {fence.pid} was reused after process registration"
            )
        if (
            current_leader.pgid != fence.pgid
            or current_leader.session_id != fence.session_id
        ):
            raise ProcessOwnershipError(
                f"pid {fence.pid} changed process-group membership"
            )

    group_members = tuple(
        identity for identity in identities if identity.pgid == fence.pgid
    )
    if any(identity.session_id != fence.session_id for identity in group_members):
        raise ProcessOwnershipError(
            f"process group {fence.pgid} was reused by another session"
        )
    return group_members


def process_group_cleanup_complete(process: asyncio.subprocess.Process) -> bool:
    """Return true only after the leader was waited and its group disappeared."""
    if getattr(process, "returncode", None) is None:
        return False
    if os.name != "posix":
        return True
    fence = get_process_group_fence(process)
    if fence is None:
        return True
    return not _owned_process_group_members(process, fence)


def _signal_owned_process_group(
    process: asyncio.subprocess.Process,
    fence: ProcessGroupFence,
    sig: signal.Signals,
) -> bool:
    # This ownership check intentionally sits immediately before killpg. It
    # proves the spawn generation plus current PGID/session membership; a bare
    # numeric process group is never sufficient authority.
    if not _owned_process_group_members(process, fence):
        return False
    assert fence.pgid is not None
    try:
        os.killpg(fence.pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise ProcessCleanupPermissionError(
            f"permission denied signalling process group {fence.pgid}"
        ) from exc
    return True


async def _wait_for_process_group_exit(
    process: asyncio.subprocess.Process,
    fence: ProcessGroupFence,
    *,
    timeout_s: float,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_s)
    while _owned_process_group_members(process, fence):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_PROCESS_GROUP_POLL_INTERVAL_S, remaining))
    return True


async def terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_s: float = 1.0,
) -> None:
    if not isinstance(process, asyncio.subprocess.Process):
        raise ProcessOwnershipError("unregistered non-asyncio Process handle")
    pid = getattr(process, "pid", None)
    leader_wait_task = asyncio.create_task(
        process.wait(),
        name=f"subprocess-leader-wait:{pid}",
    )
    fence = get_process_group_fence(process)
    signalled_group = False
    try:
        if os.name == "posix" and isinstance(pid, int) and fence is not None:
            signalled_group = _signal_owned_process_group(
                process,
                fence,
                signal.SIGTERM,
            )
            if not signalled_group:
                await leader_wait_task
                return
        elif os.name == "posix":
            raise ProcessOwnershipError(
                "refusing to signal a process without a spawn-time fence"
            )
        if signalled_group:
            assert fence is not None and fence.pgid is not None
            group_id = fence.pgid
            if await _wait_for_process_group_exit(
                process,
                fence,
                timeout_s=grace_s,
            ):
                await leader_wait_task
                return
            kill_sent = _signal_owned_process_group(
                process,
                fence,
                signal.SIGKILL,
            )
            if not kill_sent:
                await leader_wait_task
                return
            if not await _wait_for_process_group_exit(
                process,
                fence,
                timeout_s=grace_s,
            ):
                raise RuntimeError(f"process group {group_id} survived SIGKILL")
            await leader_wait_task
            return

        if leader_wait_task.done():
            await leader_wait_task
            return
        raise ProcessOwnershipError(
            "refusing to signal without POSIX procfs ownership proof"
        )
    finally:
        if not leader_wait_task.done():
            leader_wait_task.cancel()
            await asyncio.gather(leader_wait_task, return_exceptions=True)


async def wait_or_terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_s: float = 1.0,
) -> None:
    pid = getattr(process, "pid", None)
    leader_wait_task = asyncio.create_task(
        process.wait(),
        name=f"subprocess-leader-wait:{pid}",
    )
    try:
        await asyncio.wait_for(asyncio.shield(leader_wait_task), timeout=grace_s)
    except TimeoutError:
        leader_wait_task.cancel()
        await asyncio.gather(leader_wait_task, return_exceptions=True)
        await terminate_process_group(process, grace_s=grace_s)
        return
    finally:
        if not leader_wait_task.done():
            leader_wait_task.cancel()
            await asyncio.gather(leader_wait_task, return_exceptions=True)

    if not process_group_cleanup_complete(process):
        await terminate_process_group(process, grace_s=grace_s)


async def run_subprocess(
    argv: Sequence[str],
    *,
    deadline: float,
    env: Mapping[str, str],
    cwd: str | None,
    label: str,
) -> SubprocessResult:
    try:
        process = await await_before_deadline(
            spawn_process(
                argv,
                env=env,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            deadline,
        )
    except TimeoutError as exc:
        raise SubprocessDeadlineExceeded(f"{label} timed out") from exc
    try:
        stdout_b, stderr_b = await await_before_deadline(
            process.communicate(),
            deadline,
        )
    except TimeoutError as exc:
        await await_cleanup_task(
            terminate_process_group(process),
            name=f"subprocess-timeout-cleanup:{label}",
        )
        raise SubprocessDeadlineExceeded(f"{label} timed out") from exc
    except asyncio.CancelledError as cancel_exc:
        await await_cleanup_task(
            terminate_process_group(process),
            name=f"subprocess-cancel-cleanup:{label}",
            original_cancel=cancel_exc,
        )
        raise
    await await_cleanup_task(
        wait_or_terminate_process_group(process),
        name=f"subprocess-exit-cleanup:{label}",
    )
    return SubprocessResult(
        returncode=process.returncode,
        stdout=(stdout_b or b"").decode("utf-8", "replace"),
        stderr=(stderr_b or b"").decode("utf-8", "replace"),
    )

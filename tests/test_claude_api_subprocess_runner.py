from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import pytest

from services.claude_api import mcp_client, mcp_transport, subprocess_runner
from services.claude_api.subprocess_runner import (
    ProcessCleanupPermissionError,
    ProcessGroupFence,
    ProcessOwnershipError,
    build_allowlisted_env,
    deadline_after,
    run_subprocess,
    terminate_process_group,
)


class _FakeProcess:
    def __init__(self, *, hang: bool = False, pid: int = 1234) -> None:
        self.hang = hang
        self.pid = pid
        self.returncode = 0
        self.waited = False

    async def communicate(self):
        if self.hang:
            await asyncio.Event().wait()
        return b"out", b"err"

    async def wait(self):
        self.waited = True
        return self.returncode


def test_allowlisted_env_drops_ambient_secrets_and_keeps_explicit_config() -> None:
    env = build_allowlisted_env(
        {"MCP_EXPLICIT_TOKEN": "configured"},
        source={
            "HOME": "/home/test",
            "PATH": "/bin",
            "DISCORD_TOKEN": "secret",
            "ANTHROPIC_API_KEY": "secret",
            "UNRELATED": "value",
        },
    )

    assert env == {
        "HOME": "/home/test",
        "PATH": "/bin",
        "MCP_EXPLICIT_TOKEN": "configured",
    }


@pytest.mark.asyncio
async def test_deadline_never_signals_an_unregistered_fake_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(hang=True)
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        "services.claude_api.subprocess_runner.spawn_process",
        fake_spawn,
    )

    def fake_killpg(pid, sig):
        if sig == 0:
            raise ProcessLookupError
        signals.append((pid, sig))

    monkeypatch.setattr(
        "services.claude_api.subprocess_runner.os.killpg",
        fake_killpg,
    )

    with pytest.raises(ProcessOwnershipError, match="unregistered non-asyncio"):
        await run_subprocess(
            ["cli"],
            deadline=deadline_after(0.01),
            env={},
            cwd=None,
            label="cli",
        )

    assert signals == []
    assert process.waited is False


@pytest.mark.asyncio
async def test_cancellation_never_signals_an_unregistered_fake_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(hang=True)
    signals: list[signal.Signals] = []

    async def fake_spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        "services.claude_api.subprocess_runner.spawn_process",
        fake_spawn,
    )

    def fake_killpg(_pid, sig):
        if sig == 0:
            raise ProcessLookupError
        signals.append(sig)

    monkeypatch.setattr(
        "services.claude_api.subprocess_runner.os.killpg",
        fake_killpg,
    )
    task = asyncio.create_task(
        run_subprocess(
            ["cli"],
            deadline=deadline_after(60),
            env={},
            cwd=None,
            label="cli",
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert signals == []


@pytest.mark.asyncio
async def test_mcp_stdio_uses_shared_env_and_startup_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    @asynccontextmanager
    async def fake_transport(argv, *, env, cwd, deadline):
        captured.update(argv=argv, env=env, cwd=cwd, deadline=deadline)
        yield "read", "write"

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setenv("DISCORD_TOKEN", "must-not-leak")
    monkeypatch.setattr(mcp_client, "managed_stdio_client", fake_transport)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)

    async with AsyncExitStack() as stack:
        handle = await mcp_client._spawn_one(
            stack,
            "test",
            {
                "command": "server",
                "args": ["--stdio"],
                "env": {"SERVER_MODE": "test"},
                "startup_timeout": 5,
            },
        )

    assert handle.key == "test"
    assert captured["argv"] == ["server", "--stdio"]
    assert captured["env"]["SERVER_MODE"] == "test"
    assert "DISCORD_TOKEN" not in captured["env"]
    assert captured["deadline"] > asyncio.get_running_loop().time()


@pytest.mark.asyncio
async def test_mcp_startup_deadline_closes_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    @asynccontextmanager
    async def fake_transport(*_args, **_kwargs):
        nonlocal closed
        try:
            yield "read", "write"
        finally:
            closed = True

    class HangingSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(mcp_client, "managed_stdio_client", fake_transport)
    monkeypatch.setattr(mcp_client, "ClientSession", HangingSession)

    async with AsyncExitStack() as stack:
        with pytest.raises(TimeoutError):
            await mcp_client._spawn_one(
                stack,
                "slow",
                {
                    "command": "server",
                    "startup_timeout": 0.01,
                },
            )

    assert closed is True


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_mcp_transport_observer_receives_exact_spawn_handle() -> None:
    observed = []

    async with mcp_transport.managed_stdio_client(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        env=dict(os.environ),
        cwd=None,
        deadline=deadline_after(5),
        process_observer=observed.append,
    ):
        assert len(observed) == 1
        assert subprocess_runner.get_process_group_fence(observed[0]) is not None

    assert observed[0].returncode is not None


@pytest.mark.asyncio
async def test_mcp_transport_rejects_non_asyncio_observer_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(pid=2468)
    observed = []
    signals = []

    async def fake_spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(mcp_transport, "spawn_process", fake_spawn)
    monkeypatch.setattr(
        subprocess_runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(ProcessOwnershipError, match="non-asyncio"):
        async with mcp_transport.managed_stdio_client(
            ["unregistered"],
            env={},
            cwd=None,
            deadline=deadline_after(5),
            process_observer=observed.append,
        ):
            pass

    assert observed == []
    assert signals == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_mcp_transport_observer_failure_immediately_cleans_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = None
    original_spawn = mcp_transport.spawn_process
    original_cleanup = mcp_transport.wait_or_terminate_process_group

    async def capture_spawn(*args, **kwargs):
        nonlocal spawned
        spawned = await original_spawn(*args, **kwargs)
        return spawned

    async def quick_cleanup(process):
        await original_cleanup(process, grace_s=0.05)

    def reject_observer(_process):
        raise RuntimeError("observer rejected handle")

    monkeypatch.setattr(mcp_transport, "spawn_process", capture_spawn)
    monkeypatch.setattr(
        mcp_transport,
        "wait_or_terminate_process_group",
        quick_cleanup,
    )

    with pytest.raises(RuntimeError, match="observer rejected handle"):
        async with mcp_transport.managed_stdio_client(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=dict(os.environ),
            cwd=None,
            deadline=deadline_after(5),
            process_observer=reject_observer,
        ):
            pass

    assert spawned is not None
    assert spawned.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(spawned.pid, 0)


@pytest.mark.asyncio
async def test_mcp_pool_forwards_optional_process_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = []
    captured = {}

    @asynccontextmanager
    async def fake_transport(
        _argv,
        *,
        env,
        cwd,
        deadline,
        process_observer=None,
    ):
        assert env is not None
        assert cwd is None
        assert deadline > 0
        captured["observer"] = process_observer
        yield "read", "write"

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(mcp_client, "managed_stdio_client", fake_transport)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeSession)

    async with AsyncExitStack() as stack:
        pool = mcp_client.McpPool(
            stack=stack,
            available_configs={"test": {"command": "server"}},
            process_observer=observed.append,
        )
        await pool.ensure_spawned("test")

    assert captured["observer"] == observed.append
    assert observed == []


@pytest.mark.asyncio
async def test_forged_fence_on_arbitrary_handle_is_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(pid=4321)
    process.returncode = None
    setattr(
        process,
        "_kanae_process_group_fence",
        ProcessGroupFence(pid=4321, pgid=4321, generation="original"),
    )
    signals = []
    monkeypatch.setattr(
        subprocess_runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(ProcessOwnershipError, match="non-asyncio"):
        await terminate_process_group(process, grace_s=0)

    assert signals == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_unregistered_real_process_handle_is_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    signals = []
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                subprocess_runner.os,
                "killpg",
                lambda pid, sig: signals.append((pid, sig)),
            )

            with pytest.raises(ProcessOwnershipError, match="spawn-time fence"):
                await terminate_process_group(process, grace_s=0)

        assert signals == []
    finally:
        process.kill()
        await process.wait()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_pid_generation_mismatch_is_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await subprocess_runner.spawn_process(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=dict(os.environ),
        cwd=None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    fence = subprocess_runner.get_process_group_fence(process)
    assert fence is not None
    assert fence.pgid is not None
    assert fence.session_id is not None
    signals = []
    reused = subprocess_runner._ProcfsProcessIdentity(
        pid=fence.pid,
        pgid=fence.pgid,
        session_id=fence.session_id,
        generation=f"{fence.generation}-reused",
    )
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                subprocess_runner,
                "_list_process_identities",
                lambda: (reused,),
            )
            scoped.setattr(
                subprocess_runner.os,
                "killpg",
                lambda pid, sig: signals.append((pid, sig)),
            )

            with pytest.raises(ProcessOwnershipError, match="reused"):
                await terminate_process_group(process, grace_s=0)

        assert signals == []
    finally:
        process.kill()
        await process.wait()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_exited_leader_pgid_reuse_is_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await subprocess_runner.spawn_process(
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
        env=dict(os.environ),
        cwd=None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    fence = subprocess_runner.get_process_group_fence(process)
    assert fence is not None
    assert fence.pgid is not None
    assert fence.session_id is not None
    await process.wait()
    reused_member = subprocess_runner._ProcfsProcessIdentity(
        pid=fence.pid + 1,
        pgid=fence.pgid,
        session_id=fence.session_id + 1,
        generation="foreign",
    )
    signals = []
    monkeypatch.setattr(
        subprocess_runner,
        "_list_process_identities",
        lambda: (reused_member,),
    )
    monkeypatch.setattr(
        subprocess_runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(ProcessOwnershipError, match="another session"):
        await terminate_process_group(process, grace_s=0)

    assert signals == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_procfs_permission_failure_never_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await subprocess_runner.spawn_process(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=dict(os.environ),
        cwd=None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    signals = []
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                subprocess_runner,
                "_list_process_identities",
                lambda: (_ for _ in ()).throw(
                    ProcessCleanupPermissionError("procfs denied")
                ),
            )
            scoped.setattr(
                subprocess_runner.os,
                "killpg",
                lambda pid, sig: signals.append((pid, sig)),
            )

            with pytest.raises(ProcessCleanupPermissionError, match="procfs denied"):
                await terminate_process_group(process, grace_s=0)

        assert signals == []
    finally:
        process.kill()
        await process.wait()

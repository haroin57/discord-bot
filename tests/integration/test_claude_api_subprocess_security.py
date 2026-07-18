from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.shared.message import SessionMessage

from services.claude_api import mcp_client, mcp_transport, subprocess_runner, tools
from services.claude_api.subprocess_runner import (
    deadline_after,
    run_subprocess,
    spawn_process,
    terminate_process_group,
    wait_or_terminate_process_group,
)
from services.claude_api.tools import ToolContext, execute_builtin_tool
import services.deep_research.run_state as run_state_mod
from services.deep_research.run_state import ResearchRun
from services.deep_research.types import ResearchState


async def test_real_stdio_startup_failure_does_not_cancel_caller() -> None:
    """An early MCP child exit remains a normal, isolated spawn failure."""
    task = asyncio.current_task()
    assert task is not None
    cancelling_before = task.cancelling()

    async with AsyncExitStack() as stack:
        handles = await mcp_client.spawn_servers(
            {
                "early-exit": {
                    "command": sys.executable,
                    "args": ["-c", "raise SystemExit(13)"],
                    "startup_timeout": 3,
                }
            },
            stack,
        )

    assert handles == {}
    assert task.cancelling() == cancelling_before
    assert not any(
        pending.get_name().startswith("mcp-spawn-cleanup:")
        for pending in asyncio.all_tasks()
        if pending is not task and not pending.done()
    )


async def test_eager_startup_failure_isolated_from_request_task() -> None:
    """The production eager-spawn path must not cancel bot.on_message."""
    task = asyncio.current_task()
    assert task is not None
    cancelling_before = task.cancelling()

    async with AsyncExitStack() as stack:
        pool = mcp_client.McpPool(
            stack=stack,
            available_configs={
                "early-exit": {
                    "command": sys.executable,
                    "args": ["-c", "raise SystemExit(13)"],
                    "startup_timeout": 3,
                }
            },
        )
        await pool.spawn_eager(["early-exit"])

    assert pool.handles == {}
    assert task.cancelling() == cancelling_before


async def test_global_mcp_cleanup_does_not_touch_unregistered_process() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert mcp_client.kill_orphan_mcp_processes() == 0
        await asyncio.sleep(0)
        assert process.returncode is None
    finally:
        process.kill()
        await process.wait()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_process_group_cleanup_kills_descendant_after_leader_exits() -> None:
    child_code = """
import os
import signal
import time

child_pid = os.fork()
if child_pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(0)
    os.close(1)
    os.close(2)
    time.sleep(60)
    os._exit(0)

def exit_leader(*_args):
    os._exit(0)

signal.signal(signal.SIGTERM, exit_leader)
print(child_pid, flush=True)
time.sleep(60)
"""
    process = await spawn_process(
        [sys.executable, "-c", child_code],
        env=dict(os.environ),
        cwd=None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    await asyncio.wait_for(process.stdout.readline(), timeout=2)

    try:
        await terminate_process_group(process, grace_s=0.5)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.gather(process.wait(), return_exceptions=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_naturally_exited_leader_retains_safe_late_child_cleanup() -> None:
    child_code = """
import os
import signal
import time

child_pid = os.fork()
if child_pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    time.sleep(60)
    os._exit(0)

print(child_pid, flush=True)
time.sleep(0.1)
os._exit(0)
"""
    process = await spawn_process(
        [sys.executable, "-c", child_code],
        env=dict(os.environ),
        cwd=None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    child_pid = int(await asyncio.wait_for(process.stdout.readline(), timeout=2))

    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        assert process.returncode == 0

        await wait_or_terminate_process_group(process, grace_s=0.2)

        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.gather(process.wait(), return_exceptions=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_concurrent_run_cleanup_signals_exact_group_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await spawn_process(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=dict(os.environ),
        cwd=None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    run = ResearchRun(state=ResearchState(query="q"), run_id="concurrent-real")
    run.require_mcp_cleanup()
    run.register_mcp_process(process)
    stack = SimpleNamespace(aclose=AsyncMock(return_value=None))
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_calls = 0
    original_cleanup = run_state_mod.wait_or_terminate_process_group

    async def controlled_cleanup(owned_process):
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_started.set()
        await release_cleanup.wait()
        await original_cleanup(owned_process, grace_s=0.1)

    monkeypatch.setattr(
        run_state_mod,
        "wait_or_terminate_process_group",
        controlled_cleanup,
    )
    first = asyncio.create_task(run.close_owned_stack(stack.aclose))
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    shared_task = run._stack_cleanup_task
    second = asyncio.create_task(run.close_owned_stack(stack.aclose))
    await asyncio.sleep(0)

    try:
        assert run._stack_cleanup_task is shared_task
        release_cleanup.set()
        await asyncio.gather(first, second)

        assert cleanup_calls == 1
        assert stack.aclose.await_count == 1
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_run_subprocess_double_cancel_waits_for_real_process_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = asyncio.get_running_loop().create_future()
    cleanup_started = asyncio.Event()
    original_spawn = subprocess_runner.spawn_process
    original_terminate = subprocess_runner.terminate_process_group

    async def capture_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        if not spawned.done():
            spawned.set_result(process)
        return process

    async def delayed_terminate(process, *, grace_s=1.0):
        cleanup_started.set()
        await asyncio.sleep(0.1)
        await original_terminate(process, grace_s=min(grace_s, 0.2))

    monkeypatch.setattr(subprocess_runner, "spawn_process", capture_spawn)
    monkeypatch.setattr(subprocess_runner, "terminate_process_group", delayed_terminate)
    task = asyncio.create_task(
        run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            deadline=deadline_after(60),
            env=dict(os.environ),
            cwd=None,
            label="double-cancel-test",
        )
    )
    process = await asyncio.wait_for(spawned, timeout=2)
    await asyncio.sleep(0.05)

    try:
        task.cancel("original cancellation")
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        task.cancel("repeated cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

        assert raised.value.args == ("original cancellation",)
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
        assert not any(
            pending.get_name().startswith("subprocess-cancel-cleanup:")
            for pending in asyncio.all_tasks()
        )
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.gather(process.wait(), return_exceptions=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_mcp_stdio_double_cancel_closes_tasks_streams_and_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = asyncio.get_running_loop().create_future()
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    streams = {}
    original_spawn = mcp_transport.spawn_process
    original_wait = mcp_transport.wait_or_terminate_process_group

    async def capture_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        if not spawned.done():
            spawned.set_result(process)
        return process

    async def delayed_wait(process):
        cleanup_started.set()
        await asyncio.sleep(0.1)
        await original_wait(process, grace_s=0.2)

    monkeypatch.setattr(mcp_transport, "spawn_process", capture_spawn)
    monkeypatch.setattr(
        mcp_transport,
        "wait_or_terminate_process_group",
        delayed_wait,
    )

    async def hold_transport() -> None:
        async with mcp_transport.managed_stdio_client(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=dict(os.environ),
            cwd=None,
            deadline=deadline_after(5),
        ) as (read_stream, write_stream):
            streams.update(read=read_stream, write=write_stream)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_transport())
    process = await asyncio.wait_for(spawned, timeout=2)
    await asyncio.wait_for(entered.wait(), timeout=2)

    try:
        task.cancel("original cancellation")
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        task.cancel("repeated cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

        assert raised.value.args == ("original cancellation",)
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
        assert streams["read"].statistics().open_receive_streams == 0
        assert streams["read"].statistics().open_send_streams == 0
        assert streams["write"].statistics().open_receive_streams == 0
        assert streams["write"].statistics().open_send_streams == 0
        assert not any(
            pending.get_name().startswith(
                (
                    "mcp-stdio-cleanup:",
                    "mcp-stdio-reader:",
                    "mcp-stdio-writer:",
                )
            )
            for pending in asyncio.all_tasks()
        )
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.gather(process.wait(), return_exceptions=True)


async def test_mcp_stdio_accepts_json_rpc_frame_larger_than_70_kib() -> None:
    server_code = (
        "import json; "
        "print(json.dumps({'jsonrpc':'2.0','id':1,"
        "'result':{'blob':'x'*80000}}), flush=True)"
    )
    async with mcp_transport.managed_stdio_client(
        [sys.executable, "-c", server_code],
        env=dict(os.environ),
        cwd=None,
        deadline=deadline_after(5),
    ) as (read_stream, _write_stream):
        received = await asyncio.wait_for(read_stream.receive(), timeout=2)

    assert isinstance(received, SessionMessage)
    assert received.message.model_dump()["result"]["blob"] == "x" * 80000


async def test_mcp_stdio_surfaces_oversized_frame_as_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_transport, "MCP_STDIO_MAX_FRAME_BYTES", 1024)
    server_code = (
        "import json; "
        "print(json.dumps({'jsonrpc':'2.0','id':1,"
        "'result':{'blob':'x'*2048}}), flush=True)"
    )
    async with mcp_transport.managed_stdio_client(
        [sys.executable, "-c", server_code],
        env=dict(os.environ),
        cwd=None,
        deadline=deadline_after(5),
    ) as (read_stream, _write_stream):
        received = await asyncio.wait_for(read_stream.receive(), timeout=2)

    assert isinstance(received, mcp_transport.McpFrameTooLargeError)
    assert "1024 bytes" in str(received)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_bash_success_reaps_forked_descendant_that_closed_stdio(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_code = """
import os
import signal
import time

child_pid = os.fork()
if child_pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    time.sleep(60)
    os._exit(0)

print(os.getpgrp(), flush=True)
os._exit(0)
"""
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)}"
    context = ToolContext(cwd=str(tmp_path), restricted=False, channel_id=1)
    spawned_pgid = None
    original_spawn = tools.asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        nonlocal spawned_pgid
        process = await original_spawn(*args, **kwargs)
        spawned_pgid = process.pid
        return process

    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", capture_spawn)

    try:
        result = await execute_builtin_tool(
            "Bash",
            {"command": command, "timeout": 5},
            context,
        )
        reported_pgid = int(result.splitlines()[0])

        assert reported_pgid == spawned_pgid
        assert "exit code: 0" in result
        with pytest.raises(ProcessLookupError):
            os.killpg(reported_pgid, 0)
    finally:
        if spawned_pgid is not None:
            try:
                os.killpg(spawned_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
async def test_mcp_spawn_double_cancel_finishes_real_transport_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_started = asyncio.Event()
    session_cleanup_started = asyncio.Event()
    session_cleanup_finished = asyncio.Event()
    spawned = asyncio.get_running_loop().create_future()
    original_spawn = mcp_transport.spawn_process
    original_wait = mcp_transport.wait_or_terminate_process_group

    async def capture_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        if not spawned.done():
            spawned.set_result(process)
        return process

    async def quick_wait(process):
        await original_wait(process, grace_s=0.2)

    class FailingSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            session_cleanup_started.set()
            await asyncio.sleep(0.1)
            session_cleanup_finished.set()
            return False

        async def initialize(self):
            initialize_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(mcp_transport, "spawn_process", capture_spawn)
    monkeypatch.setattr(
        mcp_transport,
        "wait_or_terminate_process_group",
        quick_wait,
    )
    monkeypatch.setattr(mcp_client, "ClientSession", FailingSession)

    async with AsyncExitStack() as stack:
        task = asyncio.create_task(
            mcp_client._spawn_one(
                stack,
                "double-cancel-server",
                {
                    "command": sys.executable,
                    "args": ["-c", "import time; time.sleep(60)"],
                    "startup_timeout": 30,
                },
            )
        )
        process = await asyncio.wait_for(spawned, timeout=2)
        await asyncio.wait_for(initialize_started.wait(), timeout=2)

        try:
            task.cancel("first cancellation")
            await asyncio.wait_for(session_cleanup_started.wait(), timeout=2)
            task.cancel("second cancellation")
            with pytest.raises(asyncio.CancelledError) as caught:
                await task

            assert caught.value.args == ("first cancellation",)
            assert session_cleanup_finished.is_set()
            assert process.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(process.pid, 0)
            assert not any(
                pending.get_name().startswith(
                    (
                        "mcp-spawn-cleanup:",
                        "mcp-stdio-cleanup:",
                        "mcp-stdio-reader:",
                        "mcp-stdio-writer:",
                    )
                )
                for pending in asyncio.all_tasks()
            )
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.gather(process.wait(), return_exceptions=True)

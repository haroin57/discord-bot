"""MCP JSON-RPC stdio transport backed by the shared subprocess runner."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager

import anyio
import mcp.types as types
from mcp.shared.message import SessionMessage

from services.claude_api.subprocess_runner import (
    ProcessOwnershipError,
    await_cleanup_task,
    await_before_deadline,
    spawn_process,
    wait_or_terminate_process_group,
)

log = logging.getLogger(__name__)
MCP_STDIO_MAX_FRAME_BYTES = 8 * 1024 * 1024
ProcessObserver = Callable[
    [asyncio.subprocess.Process],
    None | Awaitable[None],
]


class McpStdioTransportError(RuntimeError):
    """Base error delivered through the MCP receive stream."""


class McpFrameTooLargeError(McpStdioTransportError):
    """A server emitted a JSON-RPC frame above the configured byte limit."""


@asynccontextmanager
async def managed_stdio_client(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    deadline: float,
    process_observer: ProcessObserver | None = None,
) -> AsyncIterator[tuple[object, object]]:
    read_send, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_receive = anyio.create_memory_object_stream(0)
    process = await await_before_deadline(
        spawn_process(
            argv,
            env=env,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            stream_limit=MCP_STDIO_MAX_FRAME_BYTES + 1,
        ),
        deadline,
    )
    if not isinstance(process, asyncio.subprocess.Process):
        raise ProcessOwnershipError(
            "MCP transport received a non-asyncio Process handle"
        )
    if process_observer is not None:
        try:
            observed = process_observer(process)
            if inspect.isawaitable(observed):
                await await_before_deadline(observed, deadline)
        except BaseException as exc:
            await await_cleanup_task(
                wait_or_terminate_process_group(process),
                name=f"mcp-observer-failure-cleanup:{process.pid}",
                original_cancel=(
                    exc if isinstance(exc, asyncio.CancelledError) else None
                ),
            )
            raise

    async def read_stdout() -> None:
        assert process.stdout is not None
        async with read_send:
            while True:
                try:
                    line = await process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    error = McpFrameTooLargeError(
                        f"MCP stdio frame exceeded {MCP_STDIO_MAX_FRAME_BYTES} bytes"
                    )
                    error.__cause__ = exc
                    await read_send.send(error)
                    return
                except Exception as exc:
                    error = McpStdioTransportError(
                        f"failed to read MCP stdio frame: {exc}"
                    )
                    error.__cause__ = exc
                    await read_send.send(error)
                    return
                if not line:
                    return
                frame = line[:-1] if line.endswith(b"\n") else line
                if frame.endswith(b"\r"):
                    frame = frame[:-1]
                if len(frame) > MCP_STDIO_MAX_FRAME_BYTES:
                    await read_send.send(
                        McpFrameTooLargeError(
                            "MCP stdio frame exceeded "
                            f"{MCP_STDIO_MAX_FRAME_BYTES} bytes"
                        )
                    )
                    return
                try:
                    message = types.JSONRPCMessage.model_validate_json(frame)
                except Exception as exc:
                    log.exception("Failed to parse MCP JSON-RPC message")
                    await read_send.send(exc)
                    continue
                await read_send.send(SessionMessage(message))

    async def write_stdin() -> None:
        assert process.stdin is not None
        async with write_receive:
            async for session_message in write_receive:
                payload = session_message.message.model_dump_json(
                    by_alias=True,
                    exclude_none=True,
                )
                process.stdin.write((payload + "\n").encode())
                await process.stdin.drain()

    reader_task = asyncio.create_task(
        read_stdout(),
        name=f"mcp-stdio-reader:{getattr(process, 'pid', 'unknown')}",
    )
    writer_task = asyncio.create_task(
        write_stdin(),
        name=f"mcp-stdio-writer:{getattr(process, 'pid', 'unknown')}",
    )
    original_cancel: asyncio.CancelledError | None = None
    try:
        yield read_stream, write_stream
    except asyncio.CancelledError as exc:
        original_cancel = exc
    finally:

        async def cleanup_transport() -> None:
            errors: list[Exception] = []

            async def capture(awaitable) -> None:
                try:
                    await awaitable
                except Exception as exc:
                    errors.append(exc)

            await capture(write_stream.aclose())
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except Exception as exc:
                    errors.append(exc)

            await capture(wait_or_terminate_process_group(process))

            for task in (reader_task, writer_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader_task, writer_task, return_exceptions=True)

            if process.stdin is not None:
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    errors.append(exc)
            await capture(read_stream.aclose())
            await capture(read_send.aclose())
            await capture(write_receive.aclose())

            if len(errors) == 1:
                raise errors[0]
            if errors:
                raise ExceptionGroup("MCP stdio cleanup failed", errors)

        await await_cleanup_task(
            cleanup_transport(),
            name=f"mcp-stdio-cleanup:{getattr(process, 'pid', 'unknown')}",
            original_cancel=original_cancel,
        )

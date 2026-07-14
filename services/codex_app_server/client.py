"""Async JSONL client for ``codex app-server`` over stdio.

The app-server wire protocol is JSON-RPC shaped, but omits the
``"jsonrpc": "2.0"`` member.  This module only owns the subprocess and wire
transport.  Thread/turn orchestration and approval policy belong to the
higher-level Discord integration.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any


class CodexAppServerError(RuntimeError):
    """A JSON-RPC error returned by Codex app-server."""

    def __init__(
        self,
        code: int,
        message: str,
        data: Any = None,
        *,
        request_id: Any = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id
        super().__init__(f"codex app-server error {code}: {message}")


class CodexProcessError(RuntimeError):
    """The Codex child process or its stdio transport became unavailable.

    The bounded stderr tail is available to callers for explicitly redacted
    diagnostics.  It is deliberately excluded from the exception string so
    an ordinary exception logger cannot disclose an OAuth token or another
    secret emitted by a child process.
    """

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stderr_tail: Sequence[str] = (),
    ) -> None:
        self.returncode = returncode
        self.stderr_tail = list(stderr_tail)
        suffix = f" (exit status {returncode})" if returncode is not None else ""
        super().__init__(f"{message}{suffix}")


_SAFE_ENV_NAMES = frozenset(
    {
        # Process discovery and basic POSIX identity.
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        # Locale, time, and temporary files.
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        # OAuth credentials may be kept in a desktop keyring reached via D-Bus.
        "DBUS_SESSION_BUS_ADDRESS",
        # TLS trust stores.
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NIX_SSL_CERT_FILE",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        # Codex/Rust diagnostics contain no credential by themselves.
        "RUST_LOG",
        "RUST_BACKTRACE",
        "NO_COLOR",
        # Common proxy spellings. Values are required in some deployments.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)

# API-key variables force a usage-billed provider route instead of the copied
# ChatGPT login. Token/secret-shaped CODEX_* variables are also excluded; the
# app-server reads the persisted OAuth session from CODEX_HOME instead.
_BLOCKED_CODEX_SECRET_PARTS = (
    "API_KEY",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "BEARER_TOKEN",
    "PASSWORD",
    "SECRET",
    "CREDENTIAL",
    "PRIVATE_KEY",
)


def _is_allowed_env_name(name: str) -> bool:
    if name in _SAFE_ENV_NAMES:
        return True
    upper = name.upper()
    if upper.startswith("CODEX_"):
        return not any(part in upper for part in _BLOCKED_CODEX_SECRET_PARTS)
    return upper.startswith(("LC_", "XDG_"))


def build_codex_env(
    base_env: Mapping[str, str] | None = None,
    *,
    codex_home: str | os.PathLike[str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the minimal environment inherited by ``codex app-server``.

    ``base_env`` defaults to the current process environment. ``overrides``
    is filtered through the same allowlist, so it cannot reintroduce Discord,
    Anthropic, OpenAI API-key, or unrelated application secrets. Supplying
    ``codex_home`` explicitly wins over both mappings and points Codex at the
    copied ChatGPT OAuth state.
    """

    source: dict[str, str] = dict(os.environ if base_env is None else base_env)
    if overrides:
        source.update(overrides)

    clean = {
        str(name): str(value)
        for name, value in source.items()
        if _is_allowed_env_name(str(name))
    }
    if codex_home is not None:
        clean["CODEX_HOME"] = os.fspath(codex_home)
    # Keep routine tracing out of stderr unless an explicitly inherited value
    # selects a different level. This is also the Codex/Hermes default.
    clean.setdefault("RUST_LOG", "warn")
    return clean


class _PendingRequest:
    __slots__ = ("method", "future")

    def __init__(self, method: str, future: asyncio.Future[dict[str, Any]]) -> None:
        self.method = method
        self.future = future


_PROCESS_ENDED = object()


class AsyncCodexAppServerClient:
    """Asynchronous Codex app-server JSONL client.

    One stdout task correlates responses and places both notifications and
    server-initiated requests on a single arrival-ordered event queue. A
    second task continuously drains stderr into a bounded diagnostic tail.
    """

    def __init__(
        self,
        codex_bin: str | os.PathLike[str] = "codex",
        codex_home: str | os.PathLike[str] | None = None,
        global_args: Sequence[str] | None = None,
        extra_args: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
        stderr_max_lines: int = 200,
        stderr_line_limit: int = 4096,
        stream_limit: int = 8 * 1024 * 1024,
    ) -> None:
        if stderr_max_lines <= 0:
            raise ValueError("stderr_max_lines must be positive")
        if stderr_line_limit <= 0:
            raise ValueError("stderr_line_limit must be positive")
        if stream_limit <= 0:
            raise ValueError("stream_limit must be positive")

        self._codex_bin = os.fspath(codex_bin)
        self._codex_home = os.fspath(codex_home) if codex_home is not None else None
        self._global_args = tuple(str(arg) for arg in (global_args or ()))
        self._extra_args = tuple(str(arg) for arg in (extra_args or ()))
        self._env = build_codex_env(env, codex_home=self._codex_home)
        self._cwd = os.fspath(cwd) if cwd is not None else None
        self._stderr_line_limit = stderr_line_limit
        self._stream_limit = stream_limit

        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._events: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=stderr_max_lines)

        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._initialize_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._initialized = False
        self._closed = False
        self._closing = False
        self._terminal_error: CodexProcessError | None = None
        self._terminal_event_enqueued = False

    async def start(self) -> AsyncCodexAppServerClient:
        """Start ``codex app-server`` once and begin draining all pipes."""

        if self._closed:
            raise RuntimeError("codex app-server client is closed")
        if self._proc is not None:
            self._require_running()
            return self

        async with self._start_lock:
            if self._closed:
                raise RuntimeError("codex app-server client is closed")
            if self._proc is not None:
                self._require_running()
                return self

            argv = (
                self._codex_bin,
                *self._global_args,
                "app-server",
                *self._extra_args,
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._env,
                    cwd=self._cwd,
                    limit=self._stream_limit,
                )
            except FileNotFoundError as exc:
                raise CodexProcessError(
                    "could not start codex app-server: executable not found"
                ) from exc
            except OSError as exc:
                # Do not include argv or env here: either may contain caller
                # supplied values that must not enter an exception logger.
                raise CodexProcessError(
                    f"could not start codex app-server: {type(exc).__name__}"
                ) from exc

            if proc.stdin is None or proc.stdout is None or proc.stderr is None:
                if proc.returncode is None:
                    proc.terminate()
                raise CodexProcessError("codex app-server stdio pipes unavailable")

            self._proc = proc
            self._stdout_task = asyncio.create_task(
                self._read_stdout(), name="codex-app-server-stdout"
            )
            self._stderr_task = asyncio.create_task(
                self._read_stderr(), name="codex-app-server-stderr"
            )
            self._wait_task = asyncio.create_task(
                self._wait_for_process(), name="codex-app-server-wait"
            )
            return self

    async def initialize(
        self,
        client_name: str = "discord_bot",
        client_title: str = "Discord Bot",
        client_version: str = "0.1.0",
        capabilities: Mapping[str, Any] | None = None,
        timeout: float | None = 10.0,
    ) -> Any:
        """Perform the required ``initialize``/``initialized`` handshake."""

        async with self._initialize_lock:
            if self._initialized:
                raise RuntimeError("codex app-server client is already initialized")
            await self.start()
            params = {
                "clientInfo": {
                    "name": client_name,
                    "title": client_title,
                    "version": client_version,
                },
                "capabilities": dict(capabilities or {}),
            }
            result = await self.request("initialize", params, timeout=timeout)
            await self.notify("initialized", {})
            self._initialized = True
            return result

    async def request(
        self,
        method: str,
        params: Any = None,
        timeout: float | None = 30.0,
    ) -> Any:
        """Send a request and return its correlated result.

        A timed-out or cancelled request is removed from the correlation map;
        a subsequent late response is ignored instead of being delivered to a
        future request.
        """

        self._require_running()
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = _PendingRequest(method, future)

        try:
            await self._send(
                {
                    "id": request_id,
                    "method": method,
                    "params": {} if params is None else params,
                }
            )
            if timeout is None:
                response = await future
            else:
                response = await asyncio.wait_for(
                    asyncio.shield(future), timeout=timeout
                )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"codex app-server method {method!r} timed out after {timeout}s"
            ) from None
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif future.cancelled() is False:
                # Retrieve a terminal exception if _send failed after the
                # process failure fan-out, avoiding an unhandled-future log.
                future.exception()

        if "error" in response:
            raw_error = response.get("error")
            if isinstance(raw_error, Mapping):
                raw_code = raw_error.get("code", -1)
                try:
                    code = int(raw_code)
                except (TypeError, ValueError):
                    code = -1
                message = str(raw_error.get("message", ""))
                data = raw_error.get("data")
            else:
                code = -1
                message = str(raw_error)
                data = None
            raise CodexAppServerError(
                code,
                message,
                data,
                request_id=response.get("id"),
            )
        return response.get("result")

    async def notify(self, method: str, params: Any = None) -> None:
        """Send a notification, which has no request id or response."""

        await self._send({"method": method, "params": {} if params is None else params})

    async def respond(self, request_id: Any, result: Any) -> None:
        """Respond successfully to a server-initiated request."""

        await self._send({"id": request_id, "result": result})

    async def respond_error(
        self,
        request_id: Any,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        """Respond with an error to a server-initiated request."""

        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._send({"id": request_id, "error": error})

    async def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next notification or server request in arrival order.

        ``None`` is returned on timeout. If the child exits, already queued
        events remain readable first and then ``CodexProcessError`` is raised.
        """

        if self._proc is None and not self._closed:
            raise RuntimeError("codex app-server client has not been started")
        if self._terminal_error is not None and self._events.empty():
            raise self._terminal_error

        try:
            if timeout is None:
                item = await self._events.get()
            elif timeout <= 0:
                item = self._events.get_nowait()
            else:
                item = await asyncio.wait_for(self._events.get(), timeout=timeout)
        except asyncio.QueueEmpty:
            return None
        except asyncio.TimeoutError:
            return None

        if item is _PROCESS_ENDED:
            if self._terminal_error is None:  # Defensive; sentinel is internal.
                raise CodexProcessError("codex app-server transport ended")
            raise self._terminal_error
        return item  # type: ignore[return-value]

    @property
    def is_alive(self) -> bool:
        """Return whether the owned subprocess is currently running."""

        return (
            self._proc is not None
            and self._proc.returncode is None
            and not self._closed
            and not self._closing
        )

    def stderr_tail(self, n: int = 20) -> list[str]:
        """Return up to the last ``n`` bounded stderr lines."""

        if n <= 0:
            return []
        return list(self._stderr_lines)[-n:]

    async def close(self, timeout: float = 3.0) -> None:
        """Close the child process, escalating from terminate to kill.

        The operation is idempotent and safe to call concurrently.
        """

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._closing = True
            self._set_terminal_error(
                CodexProcessError(
                    "codex app-server client closed",
                    returncode=(self._proc.returncode if self._proc else None),
                    stderr_tail=self.stderr_tail(),
                )
            )

            proc = self._proc
            try:
                if proc is not None:
                    writer = proc.stdin
                    if writer is not None:
                        try:
                            writer.close()
                            wait_closed = getattr(writer, "wait_closed", None)
                            if wait_closed is not None:
                                await wait_closed()
                        except (
                            BrokenPipeError,
                            ConnectionResetError,
                            RuntimeError,
                            ValueError,
                            OSError,
                        ):
                            pass

                    if proc.returncode is None:
                        try:
                            proc.terminate()
                        except OSError:
                            pass
                    try:
                        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout)
                    except asyncio.TimeoutError:
                        if proc.returncode is None:
                            try:
                                proc.kill()
                            except OSError:
                                pass
                        try:
                            await asyncio.wait_for(asyncio.shield(proc.wait()), 1.0)
                        except asyncio.TimeoutError:
                            pass
            finally:
                tasks = (self._stdout_task, self._stderr_task, self._wait_task)
                current = asyncio.current_task()
                pending_tasks = [
                    task for task in tasks if task is not None and task is not current
                ]
                for task in pending_tasks:
                    if not task.done():
                        task.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                self._closing = False

    async def __aenter__(self) -> AsyncCodexAppServerClient:
        return await self.start()

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def _send(self, message: Mapping[str, Any]) -> None:
        encoded = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        async with self._write_lock:
            self._require_running()
            assert self._proc is not None
            writer = self._proc.stdin
            if writer is None or writer.is_closing():
                error = self._new_process_error("codex app-server stdin is closed")
                self._set_terminal_error(error)
                raise error
            try:
                writer.write(encoded)
                await writer.drain()
            except (
                BrokenPipeError,
                ConnectionResetError,
                RuntimeError,
                ValueError,
                OSError,
            ) as exc:
                error = self._new_process_error(
                    f"codex app-server write failed: {type(exc).__name__}"
                )
                self._set_terminal_error(error)
                raise error from exc

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        reader = self._proc.stdout
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._append_stderr(
                        f"<non-json on codex stdout: {len(stripped)} bytes>"
                    )
                    continue
                if not isinstance(message, dict):
                    self._append_stderr("<non-object JSON on codex stdout>")
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Pipe/StreamReader failures are terminal.
            if not self._closing:
                error = self._new_process_error(
                    f"codex app-server stdout reader failed: {type(exc).__name__}"
                )
                self._set_terminal_error(error)
            return

        # stdout is the sole response channel. Losing it while the process is
        # otherwise alive is terminal even if proc.wait() has not fired.
        await asyncio.sleep(0)
        if (
            not self._closing
            and self._proc is not None
            and self._proc.returncode is None
        ):
            self._set_terminal_error(
                self._new_process_error("codex app-server stdout closed unexpectedly")
            )

    def _dispatch(self, message: dict[str, Any]) -> None:
        if (
            "id" in message
            and "method" not in message
            and ("result" in message or "error" in message)
        ):
            pending = self._pending.pop(message.get("id"), None)
            if pending is not None and not pending.future.done():
                pending.future.set_result(message)
            return

        # A single queue preserves the exact interleaving of notifications and
        # server-originated approval/tool requests.
        if "method" in message:
            self._events.put_nowait(message)
            return

        self._append_stderr("<unrecognized JSON object on codex stdout>")

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        reader = self._proc.stderr
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                self._append_stderr(line.decode("utf-8", "replace").rstrip("\r\n"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self._append_stderr(
                    f"<codex stderr reader failed: {type(exc).__name__}>"
                )

    async def _wait_for_process(self) -> None:
        assert self._proc is not None
        try:
            returncode = await self._proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self._set_terminal_error(
                    self._new_process_error(
                        f"codex app-server wait failed: {type(exc).__name__}"
                    )
                )
            return

        # Let the stderr drain consume bytes already in the pipe before the
        # bounded tail is captured on the process error.
        stderr_task = self._stderr_task
        if stderr_task is not None and not stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), 0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        if not self._closing:
            self._set_terminal_error(
                CodexProcessError(
                    "codex app-server exited unexpectedly",
                    returncode=returncode,
                    stderr_tail=self.stderr_tail(),
                )
            )

    def _append_stderr(self, line: str) -> None:
        if len(line) > self._stderr_line_limit:
            marker = "<truncated> "
            if self._stderr_line_limit <= len(marker):
                line = marker[: self._stderr_line_limit]
            else:
                line = marker + line[-(self._stderr_line_limit - len(marker)) :]
        self._stderr_lines.append(line)

    def _require_running(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("codex app-server client is closed")
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._proc is None:
            raise RuntimeError("codex app-server client has not been started")
        if self._proc.returncode is not None:
            error = self._new_process_error("codex app-server is not running")
            self._set_terminal_error(error)
            raise error

    def _new_process_error(self, message: str) -> CodexProcessError:
        return CodexProcessError(
            message,
            returncode=(self._proc.returncode if self._proc else None),
            stderr_tail=self.stderr_tail(),
        )

    def _set_terminal_error(self, error: CodexProcessError) -> None:
        if self._terminal_error is not None:
            return
        self._terminal_error = error
        pending = list(self._pending.values())
        self._pending.clear()
        for request in pending:
            if not request.future.done():
                request.future.set_exception(error)
        if not self._terminal_event_enqueued:
            self._terminal_event_enqueued = True
            self._events.put_nowait(_PROCESS_ENDED)


__all__ = [
    "AsyncCodexAppServerClient",
    "CodexAppServerError",
    "CodexProcessError",
    "build_codex_env",
]

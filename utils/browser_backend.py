"""Owned browser backends used by renderers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections import deque
from itertools import count
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

PUPPETEER_CONFIG = "/home/ubuntu/puppeteer-config.json"
PUPPETEER_MODULE = (
    "/usr/local/lib/node_modules/@mermaid-js/mermaid-cli"
    "/node_modules/puppeteer/lib/cjs/puppeteer/puppeteer.js"
)
DAEMON_SCRIPT = Path(__file__).with_name("_render_daemon.js")
DEFAULT_IDLE_TIMEOUT = 300.0
DEFAULT_RENDER_TIMEOUT = 30.0


def _find_chrome_executable() -> str | None:
    """Return the newest installed Chrome executable."""
    import glob

    patterns = [
        "/home/ubuntu/.cache/puppeteer/chrome/*/chrome-linux64/chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return sorted(matches)[-1]
    return None


def _idle_timeout_seconds() -> float:
    raw = os.getenv("RENDER_DAEMON_IDLE_TIMEOUT")
    if not raw:
        return DEFAULT_IDLE_TIMEOUT
    try:
        return max(1.0, float(raw))
    except ValueError:
        log.warning(
            "Invalid RENDER_DAEMON_IDLE_TIMEOUT=%r; using %.0fs",
            raw,
            DEFAULT_IDLE_TIMEOUT,
        )
        return DEFAULT_IDLE_TIMEOUT


def _kill_owned_browser(pid: int | None) -> None:
    """SIGKILL the Chrome launched by our daemon if it outlived the daemon.

    The pipe transport makes Chrome exit when the daemon dies, so this is a
    backstop. Chrome runs in its own session and systemd scope, so killing the
    daemon's process group does not reach it. The cmdline check guards against
    PID reuse.
    """
    if not pid:
        return
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return
    if b"chrome" not in cmdline or b"--user-data-dir=" not in cmdline:
        return
    try:
        os.kill(pid, signal.SIGKILL)
        log.warning("Killed render-daemon Chrome pid=%s left behind by its owner", pid)
    except (ProcessLookupError, PermissionError):
        pass


async def _cancel_and_await(task: asyncio.Task[Any] | None) -> None:
    """Cancel one owned task and retrieve its terminal result."""
    if task is None or task is asyncio.current_task():
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def render_remote_screenshot(
    html: str,
    *,
    token: str,
    account_id: str,
    url: str,
    timeout_seconds: float,
    retries: int,
    selector: str | None = None,
    device_scale_factor: float | None = None,
    wait_for_network_idle: bool = False,
    goto_timeout_ms: int = 20000,
    omit_background: bool = False,
) -> bytes | None:
    """Render HTML through the remote browser backend."""
    if not token or not account_id:
        log.warning("Cloudflare Browser Rendering not configured; skipping render")
        return None

    body: dict[str, Any] = {"html": html}
    if selector:
        body["selector"] = selector
    if omit_background:
        body["screenshotOptions"] = {"omitBackground": True}
    if device_scale_factor is not None:
        body["viewport"] = {
            "width": 800,
            "height": 600,
            "deviceScaleFactor": device_scale_factor,
        }
    goto_options: dict[str, Any] = {"timeout": goto_timeout_ms}
    if wait_for_network_idle:
        goto_options["waitUntil"] = "networkidle0"
    body["gotoOptions"] = goto_options

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body, headers=headers) as response:
                    if response.status == 200:
                        data = await response.read()
                        if data:
                            return data
                        log.error("Browser Rendering returned 200 but empty body")
                        return None
                    transient = response.status == 429 or response.status >= 500
                    detail = (await response.text())[:400]
                    log.error(
                        "Browser Rendering HTTP %s (attempt %d/%d): %s",
                        response.status,
                        attempt + 1,
                        attempts,
                        detail,
                    )
                    if not transient:
                        return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error(
                "Browser Rendering request failed (attempt %d/%d): %s",
                attempt + 1,
                attempts,
                exc,
            )
        if attempt < attempts - 1:
            await asyncio.sleep(1.0)
    return None


class BrowserDaemon:
    """Serialize renders through one owned Node/Puppeteer process.

    ``close`` is terminal and idempotent. Every task created by this owner is
    cancelled and awaited before ``close`` returns.
    """

    def __init__(
        self,
        *,
        idle_timeout: float | None = None,
        render_timeout: float = DEFAULT_RENDER_TIMEOUT,
    ) -> None:
        self.idle_timeout = (
            _idle_timeout_seconds() if idle_timeout is None else idle_timeout
        )
        self.render_timeout = render_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._browser_pid: int | None = None
        self._lock = asyncio.Lock()
        self._ids = count(1)
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._stderr_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._last_used = 0.0
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)[-4000:]

    async def __aenter__(self) -> BrowserDaemon:
        if self._closed:
            raise RuntimeError("browser daemon is closed")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def render(
        self,
        *,
        html_path: str,
        output_path: str,
        selector: str,
        pad: int,
        viewport: dict[str, int | float],
        wait_until: str = "domcontentloaded",
        navigation_timeout_ms: int = 5000,
    ) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("browser daemon is closed")
            await self._cancel_idle_shutdown()
            proc = await self._ensure_process()
            request_id = next(self._ids)
            request = {
                "id": request_id,
                "htmlPath": html_path,
                "outputPath": output_path,
                "selector": selector,
                "pad": pad,
                "viewport": viewport,
                "waitUntil": wait_until,
                "navigationTimeoutMs": navigation_timeout_ms,
            }

            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                proc.stdin.write(
                    (json.dumps(request, separators=(",", ":")) + "\n").encode()
                )
                await proc.stdin.drain()
                response = await asyncio.wait_for(
                    self._read_response(proc, request_id),
                    timeout=self.render_timeout,
                )
            except asyncio.CancelledError:
                await asyncio.shield(self._kill_process())
                raise
            except Exception:
                await self._kill_process()
                raise
            finally:
                self._last_used = asyncio.get_running_loop().time()
                self._schedule_idle_shutdown()

            if not response.get("ok"):
                error = response.get("error") or "unknown render daemon error"
                if self.stderr_tail:
                    log.debug("Render daemon stderr tail:\n%s", self.stderr_tail)
                raise RuntimeError(str(error))

    async def close(self) -> None:
        """Close all owned resources; repeated calls are no-ops."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._cancel_idle_shutdown()
            await self._shutdown_process()

    async def shutdown(self) -> None:
        """Compatibility alias for the former lifecycle method."""
        await self.close()

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._closed:
            raise RuntimeError("browser daemon is closed")
        if self._proc and self._proc.returncode is None:
            return self._proc

        await self._cleanup_finished_process()
        chrome = _find_chrome_executable()
        if chrome is None:
            raise RuntimeError("Chrome executable not found for local math rendering")
        cmd = [
            "node",
            str(DAEMON_SCRIPT),
            PUPPETEER_MODULE,
            PUPPETEER_CONFIG,
            chrome,
        ]
        self._stderr_tail.clear()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group so a kill reaches node and anything it forks.
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(
            self._capture_stderr(self._proc),
            name="browser-daemon-stderr",
        )
        log.debug("Started local render daemon pid=%s", self._proc.pid)
        return self._proc

    async def _read_response(
        self,
        proc: asyncio.subprocess.Process,
        request_id: int,
    ) -> dict[str, Any]:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    f"render daemon exited before response: {self.stderr_tail[:500]}"
                )
            try:
                response = json.loads(line.decode())
            except json.JSONDecodeError:
                log.warning("Ignoring non-JSON render daemon stdout: %r", line[:300])
                continue
            if response.get("event") == "browser":
                pid = response.get("pid")
                self._browser_pid = pid if isinstance(pid, int) and pid > 0 else None
                continue
            if response.get("id") == request_id:
                return response
            log.warning(
                "Ignoring render daemon response for unexpected id: %r", response
            )

    async def _capture_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(1024)
            if not chunk:
                return
            self._stderr_tail.append(chunk.decode(errors="replace"))

    async def _shutdown_process(self) -> None:
        proc = self._proc
        if not proc:
            await self._cleanup_finished_process()
            return
        if proc.returncode is None:
            try:
                assert proc.stdin is not None
                request_id = next(self._ids)
                proc.stdin.write(
                    (
                        json.dumps({"id": request_id, "method": "shutdown"}) + "\n"
                    ).encode()
                )
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.CancelledError:
                await asyncio.shield(self._kill_process())
                raise
            except Exception:
                await self._kill_process()
                return
        await self._cleanup_finished_process()

    async def _kill_process(self) -> None:
        proc = self._proc
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        await self._cleanup_finished_process()

    async def _cleanup_finished_process(self) -> None:
        stderr_task = self._stderr_task
        self._stderr_task = None
        await _cancel_and_await(stderr_task)
        self._proc = None
        _kill_owned_browser(self._browser_pid)
        self._browser_pid = None

    def _schedule_idle_shutdown(self) -> None:
        if self.idle_timeout > 0 and not self._closed:
            self._idle_task = asyncio.create_task(
                self._idle_shutdown_after_delay(),
                name="browser-daemon-idle-close",
            )

    async def _cancel_idle_shutdown(self) -> None:
        idle_task = self._idle_task
        self._idle_task = None
        await _cancel_and_await(idle_task)

    async def _idle_shutdown_after_delay(self) -> None:
        await asyncio.sleep(self.idle_timeout)
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if not self._closed and now - self._last_used >= self.idle_timeout:
                await self._shutdown_process()


_daemon = BrowserDaemon()


async def render_with_browser_daemon(**kwargs: Any) -> None:
    await _daemon.render(**kwargs)


async def close_browser_daemon() -> None:
    await _daemon.close()


async def shutdown_browser_daemon() -> None:
    """Compatibility alias for existing callers."""
    await close_browser_daemon()

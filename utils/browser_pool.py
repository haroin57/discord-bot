"""Long-lived Puppeteer browser daemon used by image renderers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from itertools import count
from pathlib import Path
from typing import Any

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
    """Find Chrome executable in puppeteer cache."""
    import glob as _glob

    patterns = [
        "/home/ubuntu/.cache/puppeteer/chrome/*/chrome-linux64/chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
    ]
    for pattern in patterns:
        matches = _glob.glob(pattern)
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
        log.warning("Invalid RENDER_DAEMON_IDLE_TIMEOUT=%r; using %.0fs", raw, DEFAULT_IDLE_TIMEOUT)
        return DEFAULT_IDLE_TIMEOUT


class BrowserDaemon:
    """A singleton-ish wrapper around a long-lived Node/Puppeteer process.

    The VPS has limited RAM, so calls are serialized while Chromium itself is
    kept warm between renders. If the daemon exits, the next render starts a
    fresh process.
    """

    def __init__(
        self,
        *,
        idle_timeout: float | None = None,
        render_timeout: float = DEFAULT_RENDER_TIMEOUT,
    ) -> None:
        self.idle_timeout = _idle_timeout_seconds() if idle_timeout is None else idle_timeout
        self.render_timeout = render_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._ids = count(1)
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._stderr_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._last_used = 0.0

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
            self._cancel_idle_shutdown()
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
                proc.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
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
                err = response.get("error") or "unknown render daemon error"
                stderr = self.stderr_tail
                if stderr:
                    log.debug("Render daemon stderr tail:\n%s", stderr)
                raise RuntimeError(str(err))

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)[-4000:]

    async def shutdown(self) -> None:
        async with self._lock:
            self._cancel_idle_shutdown()
            await self._shutdown_process()

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._proc and self._proc.returncode is None:
            return self._proc

        await self._cleanup_finished_process()
        cmd = [
            "node",
            str(DAEMON_SCRIPT),
            PUPPETEER_MODULE,
            PUPPETEER_CONFIG,
            _find_chrome_executable() or "",
        ]
        self._stderr_tail.clear()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._capture_stderr(self._proc))
        log.debug("Started render daemon pid=%s", self._proc.pid)
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
                stderr = self.stderr_tail
                raise RuntimeError(f"render daemon exited before response: {stderr[:500]}")
            try:
                response = json.loads(line.decode())
            except json.JSONDecodeError:
                log.warning("Ignoring non-JSON render daemon stdout: %r", line[:300])
                continue
            if response.get("id") == request_id:
                return response
            log.warning("Ignoring render daemon response for unexpected id: %r", response)

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
            return
        if proc.returncode is None:
            try:
                assert proc.stdin is not None
                request_id = next(self._ids)
                proc.stdin.write(
                    (json.dumps({"id": request_id, "method": "shutdown"}) + "\n").encode()
                )
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                await self._kill_process()
                return
        await self._cleanup_finished_process()

    async def _kill_process(self) -> None:
        proc = self._proc
        if proc and proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        await self._cleanup_finished_process()

    async def _cleanup_finished_process(self) -> None:
        if self._stderr_task:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=1)
            except asyncio.TimeoutError:
                self._stderr_task.cancel()
            except Exception:
                pass
            self._stderr_task = None
        self._proc = None

    def _schedule_idle_shutdown(self) -> None:
        if self.idle_timeout <= 0:
            return
        self._idle_task = asyncio.create_task(self._idle_shutdown_after_delay())

    def _cancel_idle_shutdown(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_shutdown_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.idle_timeout)
            async with self._lock:
                now = asyncio.get_running_loop().time()
                if now - self._last_used >= self.idle_timeout:
                    await self._shutdown_process()
        except asyncio.CancelledError:
            pass


_daemon = BrowserDaemon()


async def render_with_browser_daemon(**kwargs: Any) -> None:
    await _daemon.render(**kwargs)


async def shutdown_browser_daemon() -> None:
    await _daemon.shutdown()

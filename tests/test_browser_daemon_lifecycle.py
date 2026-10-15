"""Real-process lifecycle tests: the render daemon must never orphan Chrome.

Chrome launched by puppeteer runs detached and moves itself into its own
systemd scope, so the only reliable guarantee is the CDP pipe transport plus
the owner's Chrome-PID backstop. These tests drive real node + Chrome.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path

import pytest

from utils.browser_backend import (
    PUPPETEER_MODULE,
    BrowserDaemon,
    _find_chrome_executable,
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None
    or _find_chrome_executable() is None
    or not Path(PUPPETEER_MODULE).exists(),
    reason="node, puppeteer or Chrome is not installed",
)


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # A zombie has exited; only its parent still has to reap it.
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


async def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.1)
    return not _alive(pid)


async def _render_once(daemon: BrowserDaemon, tmp_path: Path) -> Path:
    html = tmp_path / "in.html"
    html.write_text("<html><body><div id='x'>E=mc^2</div></body></html>")
    out = tmp_path / "out.png"
    await daemon.render(
        html_path=str(html),
        output_path=str(out),
        selector="#x",
        pad=2,
        viewport={"width": 400, "height": 200},
    )
    return out


@pytest.mark.asyncio
async def test_render_reports_browser_pid_and_close_leaves_no_chrome(tmp_path):
    daemon = BrowserDaemon(idle_timeout=0, render_timeout=60)
    try:
        out = await _render_once(daemon, tmp_path)
        assert out.stat().st_size > 0
        chrome_pid = daemon._browser_pid
        assert chrome_pid and _alive(chrome_pid)
    finally:
        await daemon.close()
    assert await _wait_gone(chrome_pid)


@pytest.mark.asyncio
async def test_chrome_exits_when_daemon_is_sigkilled(tmp_path):
    """A hard crash of node must not leave Chrome behind (pipe transport)."""
    daemon = BrowserDaemon(idle_timeout=0, render_timeout=60)
    try:
        await _render_once(daemon, tmp_path)
        chrome_pid = daemon._browser_pid
        assert chrome_pid
        node = daemon._proc
        assert node is not None
        # Kill node only and leave the owner's backstop out of it.
        os.kill(node.pid, signal.SIGKILL)
        await node.wait()
        assert await _wait_gone(chrome_pid), "Chrome orphaned after node SIGKILL"
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_kill_process_reaps_chrome_even_if_it_ignores_the_pipe(tmp_path):
    daemon = BrowserDaemon(idle_timeout=0, render_timeout=60)
    try:
        await _render_once(daemon, tmp_path)
        chrome_pid = daemon._browser_pid
        assert chrome_pid
        # Freeze Chrome so it cannot react to the pipe closing; only the
        # owner's SIGKILL backstop can remove it now.
        os.kill(chrome_pid, signal.SIGSTOP)
        await daemon._kill_process()
        assert await _wait_gone(chrome_pid)
        assert daemon._browser_pid is None
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_daemon_restarts_cleanly_after_kill(tmp_path):
    daemon = BrowserDaemon(idle_timeout=0, render_timeout=60)
    try:
        await _render_once(daemon, tmp_path)
        first = daemon._browser_pid
        await daemon._kill_process()
        assert await _wait_gone(first)
        out = await _render_once(daemon, tmp_path)
        assert out.stat().st_size > 0
        assert daemon._browser_pid and daemon._browser_pid != first
    finally:
        await daemon.close()

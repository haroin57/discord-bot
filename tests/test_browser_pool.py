"""Tests for the local Puppeteer render daemon wrapper."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from utils.browser_pool import BrowserDaemon


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.drained = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        self.drained = True


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = object()
        self.returncode = None


@pytest.mark.asyncio
async def test_browser_daemon_kills_process_when_render_is_cancelled(monkeypatch):
    daemon = BrowserDaemon(idle_timeout=0, render_timeout=30)
    proc = _FakeProc()
    response_started = asyncio.Event()

    async def fake_ensure_process():
        return proc

    async def never_responds(_proc, _request_id):
        response_started.set()
        await asyncio.Future()

    kill_process = AsyncMock()
    monkeypatch.setattr(daemon, "_ensure_process", fake_ensure_process)
    monkeypatch.setattr(daemon, "_read_response", never_responds)
    monkeypatch.setattr(daemon, "_kill_process", kill_process)

    task = asyncio.create_task(
        daemon.render(
            html_path="/tmp/in.html",
            output_path="/tmp/out.png",
            selector="body",
            pad=0,
            viewport={"width": 1, "height": 1},
        )
    )
    await response_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    kill_process.assert_awaited_once()
    assert proc.stdin.drained
    assert b'"htmlPath":"/tmp/in.html"' in proc.stdin.writes[0]

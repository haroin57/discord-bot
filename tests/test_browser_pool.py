"""Tests for the local Puppeteer render daemon wrapper."""

import asyncio
from types import SimpleNamespace
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


@pytest.mark.asyncio
async def test_close_is_idempotent_and_awaits_idle_task():
    daemon = BrowserDaemon(idle_timeout=60)
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def owned_task():
        started.set()
        try:
            await asyncio.Future()
        finally:
            finalized.set()

    daemon._idle_task = asyncio.create_task(owned_task())
    await started.wait()

    await daemon.close()
    await daemon.close()

    assert daemon.closed is True
    assert daemon._idle_task is None
    assert finalized.is_set()


@pytest.mark.asyncio
async def test_cleanup_cancels_and_awaits_stderr_task():
    daemon = BrowserDaemon(idle_timeout=0)
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def owned_task():
        started.set()
        try:
            await asyncio.Future()
        finally:
            finalized.set()

    task = asyncio.create_task(owned_task())
    daemon._stderr_task = task
    await started.wait()

    await daemon._cleanup_finished_process()

    assert task.done()
    assert daemon._stderr_task is None
    assert finalized.is_set()


@pytest.mark.asyncio
async def test_render_rejects_use_after_close():
    daemon = BrowserDaemon(idle_timeout=0)
    await daemon.close()

    with pytest.raises(RuntimeError, match="closed"):
        await daemon.render(
            html_path="/tmp/in.html",
            output_path="/tmp/out.png",
            selector="body",
            pad=0,
            viewport={"width": 1, "height": 1},
        )


@pytest.mark.asyncio
async def test_runtime_resources_close_drains_all_owned_resources(tmp_path):
    from bot_pipeline.runtime import RuntimeResources

    background_finalized = asyncio.Event()
    notice_finalized = asyncio.Event()

    async def pending(finalized):
        try:
            await asyncio.Future()
        finally:
            finalized.set()

    background = asyncio.create_task(pending(background_finalized))
    notice = asyncio.create_task(pending(notice_finalized))
    await asyncio.sleep(0)
    rendered = tmp_path / "render.png"
    rendered.write_bytes(b"render")
    renderer = SimpleNamespace(
        tool_notice_task=notice,
        rendered_images=[str(rendered)],
        rendered_image_message_ids={str(rendered): 123},
    )
    close_browser = AsyncMock()
    resources = RuntimeResources(
        background_tasks={background},
        close_browser=close_browser,
    )
    resources.register_renderer(renderer)

    await resources.close()
    await resources.close()

    assert background_finalized.is_set()
    assert notice_finalized.is_set()
    assert not rendered.exists()
    assert renderer.rendered_images == []
    assert renderer.rendered_image_message_ids == {}
    close_browser.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_resources_reject_late_renderer_during_and_after_close(tmp_path):
    from bot_pipeline.runtime import RuntimeResources

    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def close_browser():
        close_started.set()
        await allow_close.wait()

    resources = RuntimeResources(
        background_tasks=set(),
        close_browser=close_browser,
    )
    closing = asyncio.create_task(resources.close())
    await close_started.wait()

    late_path = tmp_path / "late.png"
    late_path.write_bytes(b"render")
    late_renderer = SimpleNamespace(
        tool_notice_task=None,
        tool_notice_msg=None,
        rendered_images=[str(late_path)],
        rendered_image_message_ids={str(late_path): 1},
    )
    with pytest.raises(RuntimeError, match="closing"):
        resources.register_renderer(late_renderer)

    await resources.release_renderer(late_renderer)
    allow_close.set()
    await closing
    await resources.close()

    assert not late_path.exists()
    assert late_renderer.rendered_images == []
    with pytest.raises(RuntimeError, match="closing"):
        resources.register_renderer(late_renderer)

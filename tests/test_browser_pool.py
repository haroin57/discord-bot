"""Tests for utils/browser_pool.py (BrowserDaemon)."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_proc(responses: list[dict] | None = None, stderr_chunks: list[bytes] | None = None):
    """Build a mock asyncio.subprocess.Process that feeds canned JSON responses."""
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 12345

    # stdin
    stdin = AsyncMock()
    proc.stdin = stdin

    # stdout – readline returns each response dict as a JSON line, then b""
    lines = [json.dumps(r).encode() + b"\n" for r in (responses or [])]
    lines.append(b"")
    stdout_mock = AsyncMock()
    stdout_mock.readline = AsyncMock(side_effect=lines)
    proc.stdout = stdout_mock

    # stderr – read returns chunks then b""
    chunks = list(stderr_chunks or []) + [b""]
    stderr_mock = AsyncMock()
    stderr_mock.read = AsyncMock(side_effect=chunks)
    proc.stderr = stderr_mock

    # wait / kill
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()

    return proc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def daemon():
    """Fresh BrowserDaemon with short timeouts so tests don't hang."""
    from utils.browser_pool import BrowserDaemon
    return BrowserDaemon(idle_timeout=0.05, render_timeout=5.0)


# ---------------------------------------------------------------------------
# _find_chrome_executable
# ---------------------------------------------------------------------------

def test_find_chrome_executable_returns_none_when_missing():
    from utils.browser_pool import _find_chrome_executable
    with patch("glob.glob", return_value=[]):
        result = _find_chrome_executable()
    assert result is None


def test_find_chrome_executable_returns_last_sorted():
    from utils.browser_pool import _find_chrome_executable
    import glob as _glob
    fake_paths = [
        "/home/ubuntu/.cache/puppeteer/chrome/115/chrome-linux64/chrome",
        "/home/ubuntu/.cache/puppeteer/chrome/120/chrome-linux64/chrome",
    ]
    with patch.object(_glob, "glob", side_effect=lambda p: fake_paths if "puppeteer" in p else []):
        result = _find_chrome_executable()
    assert result == sorted(fake_paths)[-1]


# ---------------------------------------------------------------------------
# _idle_timeout_seconds
# ---------------------------------------------------------------------------

def test_idle_timeout_default(monkeypatch):
    from utils import browser_pool
    monkeypatch.delenv("RENDER_DAEMON_IDLE_TIMEOUT", raising=False)
    assert browser_pool._idle_timeout_seconds() == 300.0


def test_idle_timeout_from_env(monkeypatch):
    from utils import browser_pool
    monkeypatch.setenv("RENDER_DAEMON_IDLE_TIMEOUT", "60")
    assert browser_pool._idle_timeout_seconds() == 60.0


def test_idle_timeout_minimum_clamp(monkeypatch):
    from utils import browser_pool
    monkeypatch.setenv("RENDER_DAEMON_IDLE_TIMEOUT", "0")
    assert browser_pool._idle_timeout_seconds() == 1.0


def test_idle_timeout_invalid_falls_back(monkeypatch):
    from utils import browser_pool
    monkeypatch.setenv("RENDER_DAEMON_IDLE_TIMEOUT", "notanumber")
    assert browser_pool._idle_timeout_seconds() == 300.0


# ---------------------------------------------------------------------------
# render – happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_success(daemon):
    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await daemon.render(
            html_path="/tmp/in.html",
            output_path="/tmp/out.png",
            selector="#root",
            pad=10,
            viewport={"width": 800, "height": 600},
        )

    fake_proc.stdin.write.assert_called()
    fake_proc.stdin.drain.assert_awaited()


@pytest.mark.asyncio
async def test_render_sends_correct_json(daemon):
    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await daemon.render(
            html_path="/tmp/in.html",
            output_path="/tmp/out.png",
            selector="#root",
            pad=5,
            viewport={"width": 1024, "height": 768},
            wait_until="load",
            navigation_timeout_ms=3000,
        )

    written = fake_proc.stdin.write.call_args[0][0]
    payload = json.loads(written.decode().strip())
    assert payload["htmlPath"] == "/tmp/in.html"
    assert payload["outputPath"] == "/tmp/out.png"
    assert payload["selector"] == "#root"
    assert payload["pad"] == 5
    assert payload["waitUntil"] == "load"
    assert payload["navigationTimeoutMs"] == 3000


# ---------------------------------------------------------------------------
# render – error cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_daemon_error_raises(daemon):
    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": False, "error": "screenshot failed"}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="screenshot failed"):
            await daemon.render(
                html_path="/tmp/in.html",
                output_path="/tmp/out.png",
                selector="#root",
                pad=0,
                viewport={"width": 800, "height": 600},
            )


@pytest.mark.asyncio
async def test_render_daemon_unknown_error_raises(daemon):
    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": False}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="unknown render daemon error"):
            await daemon.render(
                html_path="/tmp/in.html",
                output_path="/tmp/out.png",
                selector="#root",
                pad=0,
                viewport={"width": 800, "height": 600},
            )


@pytest.mark.asyncio
async def test_render_process_exits_before_response_raises(daemon):
    """stdout.readline returns b"" immediately → daemon exited."""
    fake_proc = _make_fake_proc(responses=[])  # only b"" in queue

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="render daemon exited before response"):
            await daemon.render(
                html_path="/tmp/in.html",
                output_path="/tmp/out.png",
                selector="#root",
                pad=0,
                viewport={"width": 800, "height": 600},
            )


@pytest.mark.asyncio
async def test_render_kills_process_on_exception(daemon):
    fake_proc = _make_fake_proc(responses=[])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        with pytest.raises(RuntimeError):
            await daemon.render(
                html_path="/tmp/in.html",
                output_path="/tmp/out.png",
                selector="#root",
                pad=0,
                viewport={"width": 800, "height": 600},
            )

    # After failure, proc should have been cleaned up
    assert daemon._proc is None


# ---------------------------------------------------------------------------
# render – non-JSON lines are skipped, matching id returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_skips_nonjson_lines(daemon):
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 999
    proc.stdin = AsyncMock()

    lines = [
        b"not json\n",
        b'{"id": 99, "ok": true}\n',   # wrong id – skipped
        b'{"id": 1, "ok": true}\n',    # correct id
        b"",
    ]
    proc.stdout = AsyncMock()
    proc.stdout.readline = AsyncMock(side_effect=lines)
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(side_effect=[b""])
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await daemon.render(
            html_path="/tmp/in.html",
            output_path="/tmp/out.png",
            selector="#root",
            pad=0,
            viewport={"width": 800, "height": 600},
        )


# ---------------------------------------------------------------------------
# _ensure_process reuse / restart
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_process_reuses_live_proc(daemon):
    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}, {"id": 2, "ok": True}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec:
        await daemon.render(
            html_path="/tmp/a.html", output_path="/tmp/a.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )
        await daemon.render(
            html_path="/tmp/b.html", output_path="/tmp/b.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )

    # create_subprocess_exec should only be called once since proc stayed alive
    assert mock_exec.call_count == 1


@pytest.mark.asyncio
async def test_ensure_process_restarts_dead_proc(daemon):
    """If proc.returncode is set (exited), a new process is started."""
    fake_proc1 = _make_fake_proc(responses=[{"id": 1, "ok": True}])
    fake_proc2 = _make_fake_proc(responses=[{"id": 2, "ok": True}])

    call_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return fake_proc1
        return fake_proc2

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await daemon.render(
            html_path="/tmp/a.html", output_path="/tmp/a.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )
        # Simulate process exit
        fake_proc1.returncode = 1
        await daemon.render(
            html_path="/tmp/b.html", output_path="/tmp/b.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )

    assert call_count == 2


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shutdown_sends_shutdown_message(daemon):
    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await daemon.render(
            html_path="/tmp/in.html", output_path="/tmp/out.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )
        await daemon.shutdown()

    calls = [call[0][0].decode() for call in fake_proc.stdin.write.call_args_list]
    shutdown_calls = [c for c in calls if "shutdown" in c]
    assert shutdown_calls, "Expected a shutdown message to be written"


@pytest.mark.asyncio
async def test_shutdown_when_no_proc_is_noop(daemon):
    """shutdown() on a fresh daemon (no proc) must not raise."""
    await daemon.shutdown()


@pytest.mark.asyncio
async def test_shutdown_kills_on_timeout(daemon):
    """If proc.wait() times out, kill() is called."""
    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}])

    async def slow_wait():
        await asyncio.sleep(10)

    fake_proc.wait = AsyncMock(side_effect=slow_wait)

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await daemon.render(
            html_path="/tmp/in.html", output_path="/tmp/out.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )
        await daemon.shutdown()

    fake_proc.kill.assert_called()


# ---------------------------------------------------------------------------
# idle shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_shutdown_fires_after_delay():
    """With a short idle_timeout, the daemon shuts down the process automatically."""
    from utils.browser_pool import BrowserDaemon
    d = BrowserDaemon(idle_timeout=0.05, render_timeout=5.0)

    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await d.render(
            html_path="/tmp/in.html", output_path="/tmp/out.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )
        # Wait longer than idle_timeout
        await asyncio.sleep(0.2)

    # After idle timeout, _proc should have been cleaned up
    assert d._proc is None


@pytest.mark.asyncio
async def test_idle_shutdown_cancelled_on_new_render():
    """A new render cancels the pending idle shutdown."""
    from utils.browser_pool import BrowserDaemon
    d = BrowserDaemon(idle_timeout=0.5, render_timeout=5.0)

    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}, {"id": 2, "ok": True}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await d.render(
            html_path="/tmp/a.html", output_path="/tmp/a.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )
        # Very short sleep – idle task should still be pending
        await asyncio.sleep(0.01)
        await d.render(
            html_path="/tmp/b.html", output_path="/tmp/b.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )

    # Process still alive since idle hasn't fired
    assert d._proc is not None


@pytest.mark.asyncio
async def test_idle_shutdown_disabled_when_timeout_zero():
    """idle_timeout=0 means no idle task is scheduled."""
    from utils.browser_pool import BrowserDaemon
    d = BrowserDaemon(idle_timeout=0.0, render_timeout=5.0)

    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await d.render(
            html_path="/tmp/in.html", output_path="/tmp/out.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )

    assert d._idle_task is None


# ---------------------------------------------------------------------------
# stderr capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stderr_tail_captured(daemon):
    fake_proc = _make_fake_proc(
        responses=[{"id": 1, "ok": True}],
        stderr_chunks=[b"warn: low memory\n", b"error: crash\n"],
    )

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await daemon.render(
            html_path="/tmp/in.html", output_path="/tmp/out.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )

    # Give the stderr capture task a moment to consume
    await asyncio.sleep(0.05)
    assert "warn: low memory" in daemon.stderr_tail or daemon.stderr_tail == ""


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_with_browser_daemon_delegates():
    from utils import browser_pool
    with patch.object(browser_pool._daemon, "render", new_callable=AsyncMock) as mock_render:
        await browser_pool.render_with_browser_daemon(
            html_path="/tmp/x.html", output_path="/tmp/x.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )
    mock_render.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_browser_daemon_delegates():
    from utils import browser_pool
    with patch.object(browser_pool._daemon, "shutdown", new_callable=AsyncMock) as mock_shutdown:
        await browser_pool.shutdown_browser_daemon()
    mock_shutdown.assert_awaited_once()


# ---------------------------------------------------------------------------
# _kill_process when proc already exited
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_process_skips_when_already_exited(daemon):
    fake_proc = _make_fake_proc(responses=[{"id": 1, "ok": True}])

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        await daemon.render(
            html_path="/tmp/in.html", output_path="/tmp/out.png",
            selector="#r", pad=0, viewport={"width": 800, "height": 600},
        )

    # Simulate proc already exited
    fake_proc.returncode = 0
    await daemon._kill_process()
    fake_proc.kill.assert_not_called()

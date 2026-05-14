"""Integration-style tests for services.vscode_sync.daemon."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services.vscode_sync import daemon as daemon_mod


def _write_jsonl(path: Path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _append_jsonl(path: Path, events):
    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _vscode_event(**kwargs):
    base = {
        "entrypoint": "claude-vscode",
        "sessionId": "deadbeef-0000-0000-0000-000000000000",
        "cwd": "/work",
        "gitBranch": "main",
        "timestamp": "2026-05-13T12:00:00.000Z",
    }
    base.update(kwargs)
    return base


def _make_capture():
    posted: list[str] = []

    async def cb(content: str) -> None:
        posted.append(content)

    return cb, posted


@pytest.fixture(autouse=True)
def _reset_daemon_state():
    daemon_mod._state["running"] = False
    daemon_mod._state["stop_event"] = None
    yield
    daemon_mod._state["running"] = False
    daemon_mod._state["stop_event"] = None


async def test_daemon_starts_and_stops_cleanly(tmp_path: Path):
    cb, _ = _make_capture()
    task = asyncio.create_task(daemon_mod.run_daemon(
        post_callback=cb,
        projects_root=str(tmp_path),
        flush_interval=0.05,
        idle_notify_seconds=999,
        poll_interval=0.05,
    ))
    await asyncio.sleep(0.1)
    assert daemon_mod.is_running()
    await daemon_mod.stop_daemon()
    await asyncio.wait_for(task, timeout=2.0)
    assert not daemon_mod.is_running()


async def test_daemon_does_not_backfill_existing(tmp_path: Path):
    f = tmp_path / "proj" / "old.jsonl"
    _write_jsonl(f, [_vscode_event(type="user", message={"content": "ancient"})])

    cb, posted = _make_capture()
    task = asyncio.create_task(daemon_mod.run_daemon(
        post_callback=cb,
        projects_root=str(tmp_path),
        flush_interval=0.05,
        idle_notify_seconds=999,
        poll_interval=0.05,
    ))
    await asyncio.sleep(0.3)
    await daemon_mod.stop_daemon()
    await asyncio.wait_for(task, timeout=2.0)

    assert all("ancient" not in p for p in posted)


async def test_daemon_posts_new_events(tmp_path: Path):
    f = tmp_path / "proj" / "live.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)

    cb, posted = _make_capture()
    task = asyncio.create_task(daemon_mod.run_daemon(
        post_callback=cb,
        projects_root=str(tmp_path),
        flush_interval=0.05,
        idle_notify_seconds=999,
        poll_interval=0.05,
    ))
    await asyncio.sleep(0.1)

    _append_jsonl(f, [_vscode_event(type="user", message={"content": "fresh prompt"})])

    await asyncio.sleep(0.5)
    await daemon_mod.stop_daemon()
    await asyncio.wait_for(task, timeout=2.0)

    combined = "\n".join(posted)
    assert "fresh prompt" in combined


async def test_idle_session_emits_ended(tmp_path: Path):
    f = tmp_path / "proj" / "idle.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)

    cb, posted = _make_capture()
    task = asyncio.create_task(daemon_mod.run_daemon(
        post_callback=cb,
        projects_root=str(tmp_path),
        flush_interval=0.05,
        idle_notify_seconds=0,
        poll_interval=0.05,
    ))
    await asyncio.sleep(0.1)

    _append_jsonl(f, [_vscode_event(type="user", message={"content": "hello"})])
    await asyncio.sleep(0.5)
    await daemon_mod.stop_daemon()
    await asyncio.wait_for(task, timeout=2.0)

    combined = "\n".join(posted)
    assert "ended" in combined


async def test_double_start_refused(tmp_path: Path):
    cb, _ = _make_capture()
    task = asyncio.create_task(daemon_mod.run_daemon(
        post_callback=cb,
        projects_root=str(tmp_path),
        flush_interval=0.05,
        idle_notify_seconds=999,
        poll_interval=0.05,
    ))
    await asyncio.sleep(0.05)
    assert daemon_mod.is_running()

    await daemon_mod.run_daemon(
        post_callback=cb,
        projects_root=str(tmp_path),
    )
    assert daemon_mod.is_running()

    await daemon_mod.stop_daemon()
    await asyncio.wait_for(task, timeout=2.0)


async def test_tailer_exception_does_not_kill_daemon(tmp_path: Path):
    cb, _ = _make_capture()
    with patch("services.vscode_sync.daemon.JSONLTailer") as MockTailer:
        instance = MockTailer.return_value
        instance.prime.return_value = None
        instance._offsets = {}
        call_count = {"n": 0}

        def poll():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("tailer boom")
            return []

        instance.poll.side_effect = poll

        task = asyncio.create_task(daemon_mod.run_daemon(
            post_callback=cb,
            projects_root=str(tmp_path),
            flush_interval=0.05,
            idle_notify_seconds=999,
            poll_interval=0.05,
        ))
        await asyncio.sleep(0.3)
        assert daemon_mod.is_running()
        assert call_count["n"] >= 2
        await daemon_mod.stop_daemon()
        await asyncio.wait_for(task, timeout=2.0)

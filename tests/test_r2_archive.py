"""Tests for the durable Cloudflare R2 archive spool."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import config
from services import r2_archive as r2


def _enable(monkeypatch, tmp_path):
    state = tmp_path / ".state"
    state.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", str(state))
    monkeypatch.setattr(config, "R2_ARCHIVE_ENABLED", True)
    monkeypatch.setattr(config, "R2_ARCHIVE_BUCKET", "test-bucket")
    monkeypatch.setattr(config, "R2_ARCHIVE_PREFIX", "bot/v1")
    return state


def _manifests(state: Path):
    return sorted((state / "r2_archive" / "pending").glob("*.json"))


def test_history_event_is_individually_queued(monkeypatch, tmp_path):
    state = _enable(monkeypatch, tmp_path)
    ref = r2.archive_history_entry(123456789012345, {
        "role": "user", "content": "hello", "timestamp": "2026-07-12T12:00:00+0900",
    })
    assert ref is not None
    assert ref.key.startswith("bot/v1/history/events/123456789012345/")
    manifest = json.loads(_manifests(state)[0].read_text())
    payload = _manifests(state)[0].with_name(manifest["payload"])
    saved = json.loads(payload.read_text())
    assert saved["content"] == "hello"
    assert saved["channel_id"] == 123456789012345
    assert saved["archive_event_id"]


def test_image_is_copied_before_source_cleanup_and_is_discoverable(monkeypatch, tmp_path):
    state = _enable(monkeypatch, tmp_path)
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    ref = r2.archive_image(
        image,
        channel_id=123456789012345,
        message_id=987654321098765,
        direction="outbound-generated",
        author_id=42,
    )
    assert ref is not None
    image.unlink()
    payloads = list((state / "r2_archive" / "pending").glob("*.payload"))
    assert any(p.read_bytes().startswith(b"\x89PNG") for p in payloads)
    recent = r2.recent_images(123456789012345)
    assert recent[0]["uri"] == ref.uri
    assert recent[0]["message_id"] == 987654321098765


def test_state_sync_uses_sqlite_backup_and_clean_layout(monkeypatch, tmp_path):
    state = _enable(monkeypatch, tmp_path)
    (state / "runtime_state.json").write_text('{"ok":true}')
    history = state / "history"
    history.mkdir()
    (history / "123456789012345.jsonl").write_text('{"role":"user"}\n')
    (state / "bot.pid").write_text("123")
    (state / "data.sqlite-wal").write_text("transient")
    (state / "data.sqlite-shm").write_text("transient")
    db = sqlite3.connect(state / "data.sqlite")
    db.execute("create table items(value text)")
    db.execute("insert into items values ('saved')")
    db.commit()
    db.close()

    report = r2.sync_state_once()
    assert report["changed"] == 3
    keys = [json.loads(p.read_text())["key"] for p in _manifests(state)]
    assert "bot/v1/state/latest/runtime_state.json" in keys
    assert "bot/v1/history/transcripts/123456789012345.jsonl" in keys
    assert "bot/v1/state/latest/data.sqlite" in keys
    assert not any(key.endswith("bot.pid") for key in keys)
    assert not any(key.endswith(("-wal", "-shm")) for key in keys)
    assert any("state/manifests/" in key for key in keys)
    assert "bot/v1/state/latest-manifest.json" in keys


def test_flush_removes_successful_spool_items(monkeypatch, tmp_path):
    state = _enable(monkeypatch, tmp_path)
    r2.archive_history_entry(123456789012345, {"role": "user", "content": "x"})
    calls = []

    def fake_run(args, timeout=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(r2, "_run_wrangler", fake_run)
    result = r2.flush_pending_once(limit=10)
    assert result["uploaded"] == 1
    assert result["pending"] == 0
    assert calls[0][:3] == ["r2", "object", "put"]
    assert not list((state / "r2_archive" / "pending").glob("*"))
    catalog = (state / "r2_archive" / "catalog.jsonl").read_text()
    assert '"kind":"delivery"' in catalog


def test_recent_image_reports_uploaded_after_delivery(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    r2.archive_image(
        image,
        channel_id=123456789012345,
        message_id=987654321098765,
        direction="inbound",
    )
    assert r2.recent_images(123456789012345)[0]["upload_status"] == "pending"
    monkeypatch.setattr(
        r2,
        "_run_wrangler",
        lambda args, timeout=None: subprocess.CompletedProcess(args, 0, stdout="ok", stderr=""),
    )
    r2.flush_pending_once(limit=10)
    assert r2.recent_images(123456789012345)[0]["upload_status"] == "uploaded"


def test_fetch_accepts_own_uri_and_rejects_other_prefix(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)

    def fake_run(args, timeout=None):
        target = Path(args[args.index("--file") + 1])
        target.write_bytes(b"downloaded")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(r2, "_run_wrangler", fake_run)
    out = tmp_path / "out.png"
    fetched = r2.fetch_object("r2://test-bucket/bot/v1/images/a.png", out)
    assert fetched.read_bytes() == b"downloaded"
    try:
        r2.normalize_object_key("other-prefix/secret")
    except ValueError:
        pass
    else:
        raise AssertionError("foreign prefix was accepted")


def test_process_lock_is_non_blocking_across_file_descriptors(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    with r2._process_lock("flush") as first:
        assert first is True
        with r2._process_lock("flush") as second:
            assert second is False

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


def _catalog_rows(state: Path, where: str = "1=1") -> list[dict]:
    with sqlite3.connect(state / "r2_catalog.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM objects WHERE {where} ORDER BY created_at, object_key"
            )
        ]


def test_history_event_is_individually_queued(monkeypatch, tmp_path):
    state = _enable(monkeypatch, tmp_path)
    ref = r2.archive_history_entry(
        123456789012345,
        {
            "role": "user",
            "content": "hello",
            "timestamp": "2026-07-12T12:00:00+0900",
        },
    )
    assert ref is not None
    assert ref.key.startswith("bot/v1/history/events/123456789012345/")
    manifest = json.loads(_manifests(state)[0].read_text())
    payload = _manifests(state)[0].with_name(manifest["payload"])
    saved = json.loads(payload.read_text())
    assert saved["content"] == "hello"
    assert saved["channel_id"] == 123456789012345
    assert saved["archive_event_id"]
    assert (state / "r2_catalog.sqlite").stat().st_mode & 0o777 == 0o600


def test_image_is_copied_before_source_cleanup_and_is_discoverable(
    monkeypatch, tmp_path
):
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
    assert recent[0]["request_message_id"] == 987654321098765
    assert recent[0]["response_message_id"] is None
    assert recent[0]["status"] == "pending"
    assert recent[0]["retry_count"] == 0
    assert recent[0]["sha256"] == ref.sha256


def test_image_catalog_stores_attachment_and_separate_message_ids(
    monkeypatch, tmp_path
):
    state = _enable(monkeypatch, tmp_path)
    image = tmp_path / "987654321012345678.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)

    ref = r2.archive_image(
        image,
        channel_id=123456789012345,
        request_message_id=111111111111111,
        response_message_id=222222222222222,
        attachment_id=987654321012345678,
        original_name="original chart.png",
        direction="outbound-generated",
    )

    assert ref is not None
    rows = _catalog_rows(state, "kind='image'")
    assert len(rows) == 1
    row = rows[0]
    assert row["channel_id"] == 123456789012345
    assert row["message_id"] == 222222222222222
    assert row["request_message_id"] == 111111111111111
    assert row["response_message_id"] == 222222222222222
    assert row["attachment_id"] == "987654321012345678"
    assert row["object_key"] == ref.key
    assert row["sha256"] == ref.sha256
    assert row["status"] == "pending"
    assert row["retry_count"] == 0


def test_image_request_id_is_a_durable_unique_spool_key(monkeypatch, tmp_path):
    state = _enable(monkeypatch, tmp_path)
    image = tmp_path / "generated.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)

    first = r2.archive_image(
        image,
        channel_id=123,
        request_message_id=111,
        direction="outbound-generated",
        idempotency_key="request-1:archive:0",
    )
    second = r2.archive_image(
        image,
        channel_id=123,
        request_message_id=222,
        direction="outbound-generated",
        idempotency_key="request-1:archive:0",
    )

    assert first is not None and second is not None
    assert first.key == second.key
    assert len(_catalog_rows(state, "kind='image'")) == 1
    assert len(_manifests(state)) == 2  # image plus metadata, each queued once


def test_bind_response_message_updates_catalog_and_pending_manifest(
    monkeypatch, tmp_path
):
    state = _enable(monkeypatch, tmp_path)
    image = tmp_path / "generated.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    ref = r2.archive_image(
        image,
        channel_id=123456789012345,
        request_message_id=111111111111111,
        direction="outbound-generated",
    )
    assert ref is not None

    assert r2.bind_response_message([ref], 222222222222222) == 1
    row = _catalog_rows(state, "kind='image'")[0]
    assert row["request_message_id"] == 111111111111111
    assert row["response_message_id"] == 222222222222222
    assert row["message_id"] == 222222222222222
    manifest = next(
        json.loads(path.read_text())
        for path in _manifests(state)
        if json.loads(path.read_text())["key"] == ref.key
    )
    assert manifest["metadata"]["request_message_id"] == 111111111111111
    assert manifest["metadata"]["response_message_id"] == 222222222222222
    metadata_manifest = next(
        json.loads(path.read_text())
        for path in _manifests(state)
        if json.loads(path.read_text())["key"] == f"{ref.key}.metadata.json"
    )
    metadata_payload = json.loads(
        (state / "r2_archive" / "pending" / metadata_manifest["payload"]).read_text()
    )
    assert metadata_payload["request_message_id"] == 111111111111111
    assert metadata_payload["response_message_id"] == 222222222222222
    companion = _catalog_rows(state, "kind='image_metadata'")[0]
    assert companion["response_message_id"] == 222222222222222
    assert companion["sha256"] == metadata_manifest["sha256"]


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
    row = _catalog_rows(state, "kind='history_event'")[0]
    assert row["status"] == "uploaded"
    assert row["retry_count"] == 0
    assert row["uploaded_at"] is not None


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
        lambda args, timeout=None: subprocess.CompletedProcess(
            args, 0, stdout="ok", stderr=""
        ),
    )
    r2.flush_pending_once(limit=10)
    assert r2.recent_images(123456789012345)[0]["upload_status"] == "uploaded"


def test_failed_upload_records_retry_status(monkeypatch, tmp_path):
    state = _enable(monkeypatch, tmp_path)
    ref = r2.archive_history_entry(123456789012345, {"role": "user", "content": "x"})
    assert ref is not None
    monkeypatch.setattr(
        r2,
        "_run_wrangler",
        lambda args, timeout=None: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="network unavailable"
        ),
    )

    result = r2.flush_pending_once(limit=10)

    assert result["failed"] == 1
    row = _catalog_rows(state, f"object_key='{ref.key}'")[0]
    assert row["status"] == "retry_wait"
    assert row["retry_count"] == 1
    assert row["last_error"] == "network unavailable"

    manifest_path = _manifests(state)[0]
    manifest = json.loads(manifest_path.read_text())
    manifest["next_attempt_at"] = 0
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(
        r2,
        "_run_wrangler",
        lambda args, timeout=None: subprocess.CompletedProcess(
            args, 0, stdout="ok", stderr=""
        ),
    )
    success = r2.flush_pending_once(limit=10)
    assert success["uploaded"] == 1
    delivered = _catalog_rows(state, f"object_key='{ref.key}'")[0]
    assert delivered["status"] == "uploaded"
    assert delivered["retry_count"] == 1
    assert delivered["last_error"] is None


def test_legacy_jsonl_catalog_is_migrated_once(monkeypatch, tmp_path):
    state = _enable(monkeypatch, tmp_path)
    legacy_dir = state / "r2_archive"
    legacy_dir.mkdir()
    key = "bot/v1/images/123/2026/07/12/456/inbound/hash-789.png"
    legacy_rows = [
        {
            "bucket": "test-bucket",
            "key": key,
            "kind": "image",
            "sha256": "abc123",
            "size": 42,
            "content_type": "image/png",
            "created_at": "2026-07-12T00:00:00.000Z",
            "channel_id": 123,
            "message_id": 456,
            "direction": "inbound",
            "metadata": {
                "channel_id": 123,
                "message_id": 456,
                "original_name": "789.png",
            },
        },
        {"kind": "delivery", "key": key},
    ]
    (legacy_dir / "catalog.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in legacy_rows)
    )

    first = r2.recent_images(123)
    second = r2.recent_images(123)

    assert len(first) == len(second) == 1
    assert first[0]["attachment_id"] == "789"
    assert first[0]["request_message_id"] == 456
    assert first[0]["response_message_id"] is None
    assert first[0]["status"] == "uploaded"
    with sqlite3.connect(state / "r2_catalog.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM catalog_meta WHERE key='legacy_jsonl_migrated'"
            ).fetchone()[0]
            == 1
        )


def test_sqlite_fingerprint_includes_wal_changes(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    db_path = tmp_path / "catalog.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE items(value TEXT)")
        first = r2._fingerprint(db_path)
        conn.execute("INSERT INTO items VALUES ('changed')")
        conn.commit()
        second = r2._fingerprint(db_path)

    assert "wal:" in second
    assert second != first


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

"""Archive ACK validation, bounded transfers, and durable-spool fairness."""

import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest

import config
from services import r2_archive as r2
from services.offload.archive_transport import ArchiveHTTPTransport, ArchiveTransportError


def transport(handler):
    client = ArchiveHTTPTransport("https://archive.example", "token" * 12)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def test_upload_streams_and_checks_ack(tmp_path):
    path = tmp_path / "payload"
    path.write_bytes(b"content")
    manifest = {"key": "bot/v1/test", "sha256": hashlib.sha256(b"content").hexdigest(), "size": 7, "queue_id": "revision"}
    def handler(request):
        assert request.read() == b"content"
        assert request.headers["Content-Length"] == "7"
        assert request.headers["X-Archive-Revision"] == "revision"
        return httpx.Response(200, json={"ok": True, "size": 7, "sha256": manifest["sha256"]})
    with transport(handler) as client:
        client.upload(manifest, path)


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"Location": "https://evil.test"}), httpx.Response(200, json={"ok": True, "size": 7, "sha256": "wrong"}), httpx.Response(200, content=b"x" * 8192)])
def test_unconfirmed_uploads_fail_without_following_redirects(tmp_path, response):
    path = tmp_path / "payload"
    path.write_bytes(b"content")
    manifest = {"key": "bot/v1/test", "sha256": "0" * 64, "size": 7, "queue_id": "revision"}
    with transport(lambda request: response) as client, pytest.raises(ArchiveTransportError):
        client.upload(manifest, path)


def test_corrupt_download_preserves_existing_destination(tmp_path):
    path = tmp_path / "output"
    path.write_bytes(b"previous")
    def handler(request):
        return httpx.Response(200, stream=httpx.ByteStream(b"bad"), headers={"Content-Length": "3", "X-Archive-SHA256": "0" * 64})
    with transport(handler) as client, pytest.raises(ArchiveTransportError):
        client.download("bot/v1/test", path)
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.glob(".r2-download-*")) == []


def test_download_is_private_and_atomic(tmp_path):
    path = tmp_path / "output"
    with transport(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"abc"), headers={"Content-Length": "3", "X-Archive-SHA256": hashlib.sha256(b"abc").hexdigest()})) as client:
        client.download("bot/v1/test", path)
    assert path.read_bytes() == b"abc"
    assert path.stat().st_mode & 0o777 == 0o600


def enable(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "R2_ARCHIVE_ENABLED", True)
    monkeypatch.setattr(config, "R2_ARCHIVE_TRANSPORT", "wrangler")
    monkeypatch.setattr(config, "R2_ARCHIVE_BUCKET", "test")
    monkeypatch.setattr(config, "R2_ARCHIVE_PREFIX", "bot/v1")


def queue(key, text):
    return r2._queue_payload(key="bot/v1/" + key, kind="state_file", content_type="text/plain", data=text.encode())


def test_backoff_does_not_starve_other_keys_but_preserves_same_key_order(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    queue("snapshot", "old")
    queue("snapshot", "new")
    queue("independent", "deliver")
    manifests = sorted(r2._pending_dir().glob("*.json"))
    data = json.loads(manifests[0].read_text())
    data["next_attempt_at"] = 1e20
    manifests[0].write_text(json.dumps(data))
    calls = []
    def upload(args):
        calls.append(args[3])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(r2, "_run_wrangler", upload)
    assert r2.flush_pending_once(limit=1)["uploaded"] == 1
    assert calls == ["test/bot/v1/independent"]
    assert manifests[0].exists() and manifests[1].exists()


def test_failed_http_upload_keeps_manifest_payload_and_blocks_newer_same_key(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    queue("snapshot", "old")
    queue("snapshot", "new")
    class Offline:
        def upload(self, manifest, path):
            raise ArchiveTransportError("archive HTTP 503")
    result = r2._flush_pending_once_locked(limit=20, transport=Offline())
    assert result["failed"] == 1 and result["uploaded"] == 0
    assert len(list(r2._pending_dir().glob("*.payload"))) == 2


def test_http_batch_reuses_transport_without_spawning_cli(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    queue("first", "one")
    queue("second", "two")
    calls = []
    class Online:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def upload(self, manifest, path): calls.append(manifest["key"])
    monkeypatch.setattr(r2, "_http_transport", lambda: Online())
    monkeypatch.setattr(r2, "_run_wrangler", lambda *a: pytest.fail("CLI spawned"))
    result = r2.flush_pending_once()
    assert result["uploaded"] == 2 and result["pending"] == 0
    assert calls == ["bot/v1/first", "bot/v1/second"]


def test_state_scan_prunes_spool_and_does_not_follow_symlinks(tmp_path):
    (tmp_path / "r2_archive" / "pending").mkdir(parents=True)
    (tmp_path / "r2_archive" / "pending" / "never-scan").write_text("spool")
    (tmp_path / "normal").write_text("state")
    (tmp_path / "link").symlink_to(tmp_path / "normal")
    assert list(r2._iter_state_files(tmp_path)) == [tmp_path / "normal"]

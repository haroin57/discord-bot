"""Tests for services/health.py (SRE-C7)."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import sys
import time
from urllib.error import URLError
from unittest.mock import MagicMock, patch

import pytest

import services.health as health_service
from services.health import (
    get_health_snapshot,
    get_liveness_snapshot,
    get_readiness_snapshot,
    uptime_seconds,
)


def test_uptime_increases_over_time():
    before = uptime_seconds()
    time.sleep(0.05)
    after = uptime_seconds()
    assert after >= before


def test_health_snapshot_keys():
    snapshot = get_readiness_snapshot(bot=None)
    assert "status" in snapshot
    assert "uptime_s" in snapshot
    assert "subsystems" in snapshot
    assert set(snapshot["subsystems"].keys()) == {
        "discord",
        "oauth",
        "mem0",
        "release",
        "system",
    }
    assert "build_sha" in snapshot
    assert "release_sha" in snapshot


def test_health_degraded_when_discord_not_ready():
    bot = MagicMock()
    bot.is_ready.return_value = False

    with (
        patch("services.health._check_oauth", return_value={"ok": True}),
        patch("services.health._check_mem0", return_value={"ok": True}),
        patch("services.health._check_system", return_value={"ok": True}),
        patch(
            "services.health._check_release",
            return_value={"ok": True, "sha": "a" * 40},
        ),
    ):
        snapshot = get_readiness_snapshot(bot=bot)

    assert snapshot["status"] == "degraded"
    assert snapshot["subsystems"]["discord"]["ok"] is False


def test_health_ok_when_all_subsystems_ok():
    bot = MagicMock()
    bot.is_ready.return_value = True
    bot.latency = 0.042

    with (
        patch("services.health._check_oauth", return_value={"ok": True}),
        patch("services.health._check_mem0", return_value={"ok": True}),
        patch("services.health._check_system", return_value={"ok": True}),
        patch(
            "services.health._check_release",
            return_value={"ok": True, "sha": "a" * 40},
        ),
    ):
        snapshot = get_readiness_snapshot(bot=bot)

    assert snapshot["status"] == "ok"
    assert snapshot["subsystems"]["discord"]["ok"] is True
    assert snapshot["subsystems"]["discord"]["gateway_latency_ms"] == 42
    assert snapshot["release_sha"] == "a" * 40
    assert snapshot["build_sha"] == "a" * 40


def test_health_is_unready_while_discord_jobs_are_draining():
    bot = MagicMock()
    bot.is_ready.return_value = True
    bot.accepting_jobs = lambda: False
    bot.latency = 0.01

    with (
        patch("services.health._check_oauth", return_value={"ok": True}),
        patch("services.health._check_mem0", return_value={"ok": True}),
        patch("services.health._check_system", return_value={"ok": True}),
        patch(
            "services.health._check_release",
            return_value={"ok": True, "sha": "a" * 40},
        ),
    ):
        snapshot = get_readiness_snapshot(bot=bot)

    assert snapshot["status"] == "degraded"
    assert snapshot["subsystems"]["discord"] == {
        "ok": False,
        "gateway_latency_ms": 10,
        "reason": "shutdown_draining",
    }


def test_liveness_does_not_probe_dependencies():
    with (
        patch("services.health._check_oauth", side_effect=AssertionError),
        patch("services.health._check_mem0", side_effect=AssertionError),
        patch("services.health._check_system", side_effect=AssertionError),
        patch("services.health._check_release", side_effect=AssertionError),
    ):
        snapshot = get_liveness_snapshot()

    assert snapshot["status"] == "live"
    assert snapshot["uptime_s"] >= 0
    assert "subsystems" not in snapshot


def test_health_snapshot_is_readiness_compatibility_alias():
    bot = MagicMock()
    with patch(
        "services.health.get_readiness_snapshot",
        return_value={"status": "degraded", "uptime_s": 1, "subsystems": {}},
    ) as readiness:
        snapshot = get_health_snapshot(bot=bot)

    assert snapshot["status"] == "degraded"
    readiness.assert_called_once_with(bot=bot)


def test_check_system_reports_metrics():
    from services.health import _check_system

    snapshot = _check_system()
    assert "cpu_load_pct" in snapshot
    assert "mem_pct" in snapshot
    assert "disk_pct" in snapshot
    assert "swap_pct" in snapshot


def test_oauth_import_error_fails_readiness_closed():
    from services.health import _check_oauth

    with patch.dict(sys.modules, {"services.oauth_manager": None}):
        snapshot = _check_oauth()

    assert snapshot == {"ok": False, "reason": "oauth_module_unavailable"}


def test_release_identity_unavailable_fails_readiness_closed():
    bot = MagicMock()
    bot.is_ready.return_value = True

    with (
        patch("services.health._check_oauth", return_value={"ok": True}),
        patch("services.health._check_mem0", return_value={"ok": True}),
        patch("services.health._check_system", return_value={"ok": True}),
        patch("services.health._RELEASE_SHA", None),
        patch("services.health._RELEASE_ERROR", "release_sha_unavailable"),
    ):
        snapshot = get_readiness_snapshot(bot=bot)

    assert snapshot["status"] == "degraded"
    assert snapshot["release_sha"] is None
    assert snapshot["subsystems"]["release"] == {
        "ok": False,
        "sha": None,
        "reason": "release_sha_unavailable",
    }


def _release_metadata(tmp_path: Path, monkeypatch, *, sha: str = "a" * 40) -> Path:
    release_dir = tmp_path / "release"
    release_dir.mkdir(mode=0o755)
    generations = release_dir / "generations"
    generation = generations / sha
    (generation / "src").mkdir(parents=True, mode=0o755)
    (generation / "venv").mkdir(mode=0o755)
    for directory in (
        release_dir,
        generations,
        generation,
        generation / "src",
        generation / "venv",
    ):
        directory.chmod(0o755)
    (generation / ".release-sha").write_text(sha + "\n", encoding="ascii")
    (generation / "src/.release-source-sha").write_text(sha + "\n", encoding="ascii")
    (generation / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (generation / ".release-sha").chmod(0o644)
    (generation / "src/.release-source-sha").chmod(0o644)
    (generation / "src/app.py").chmod(0o644)
    content_manifest = generation / "release-content.jsonl"
    content_manifest.write_text(
        "".join(
            json.dumps(
                {
                    "mode": 0o644,
                    "path": content_path.relative_to(generation).as_posix(),
                    "sha256": hashlib.sha256(content_path.read_bytes()).hexdigest(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
            for content_path in (
                generation / "src/.release-source-sha",
                generation / "src/app.py",
            )
        ),
        encoding="utf-8",
    )
    content_manifest.chmod(0o644)
    metadata = generation / "release.json"
    metadata.write_text(
        json.dumps(
            {
                "format": "discord-bot-release-v2",
                "release_sha": sha,
                "generation": sha,
                "content_manifest_sha256": hashlib.sha256(
                    content_manifest.read_bytes()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    metadata.chmod(0o644)
    current = release_dir / "current"
    current.symlink_to(Path("generations") / sha)
    monkeypatch.setenv("BOT_RELEASE_ROOT", str(release_dir))
    monkeypatch.setenv("BOT_RELEASE_METADATA_PATH", str(current / "release.json"))
    monkeypatch.setenv("BOT_RELEASE_METADATA_OWNER", pwd.getpwuid(os.getuid()).pw_name)
    return metadata


def test_health_import_does_not_hash_release_generation(tmp_path, monkeypatch):
    import importlib

    _release_metadata(tmp_path, monkeypatch)
    with patch.object(
        Path,
        "read_bytes",
        side_effect=AssertionError("module import must not hash generation content"),
    ):
        importlib.reload(health_service)

    assert health_service._RELEASE_SHA is None
    assert health_service._RELEASE_ERROR == "release_verification_pending"
    assert health_service._RELEASE_VERIFICATION_STARTED is False


def test_background_release_verification_transitions_pending_to_ready(monkeypatch):
    monkeypatch.setattr(health_service, "_RELEASE_SHA", None)
    monkeypatch.setattr(
        health_service, "_RELEASE_ERROR", "release_verification_pending"
    )
    monkeypatch.setattr(health_service, "_RELEASE_VERIFICATION_STARTED", False)
    monkeypatch.setattr(
        health_service,
        "_resolve_release_sha",
        MagicMock(return_value=("a" * 40, None)),
    )

    assert health_service._check_release() == {
        "ok": False,
        "sha": None,
        "reason": "release_verification_pending",
    }
    assert health_service.initialize_release_verification() == ("a" * 40, None)
    assert health_service._check_release() == {"ok": True, "sha": "a" * 40}


def test_background_release_verification_failure_remains_fail_closed(monkeypatch):
    monkeypatch.setattr(health_service, "_RELEASE_SHA", None)
    monkeypatch.setattr(
        health_service, "_RELEASE_ERROR", "release_verification_pending"
    )
    monkeypatch.setattr(health_service, "_RELEASE_VERIFICATION_STARTED", False)
    monkeypatch.setattr(
        health_service,
        "_resolve_release_sha",
        MagicMock(return_value=(None, "release_content_mismatch")),
    )

    assert health_service.initialize_release_verification() == (
        None,
        "release_content_mismatch",
    )
    assert health_service._check_release() == {
        "ok": False,
        "sha": None,
        "reason": "release_content_mismatch",
    }


def test_release_identity_reads_protected_metadata_without_git(tmp_path, monkeypatch):
    from services.health import _resolve_release_sha

    _release_metadata(tmp_path, monkeypatch)
    assert _resolve_release_sha() == ("a" * 40, None)


def test_release_identity_rejects_writable_or_symlink_metadata(tmp_path, monkeypatch):
    from services.health import _resolve_release_sha

    metadata = _release_metadata(tmp_path, monkeypatch)
    metadata.chmod(0o666)
    assert _resolve_release_sha() == (None, "release_metadata_writable")

    metadata.chmod(0o644)
    metadata.parent.chmod(0o777)
    assert _resolve_release_sha() == (None, "release_generation_writable")

    metadata.parent.chmod(0o755)
    other = metadata.parent / "other.json"
    metadata.rename(other)
    metadata.symlink_to(other.name)
    assert _resolve_release_sha() == (None, "release_metadata_generation_mismatch")


def test_release_identity_rejects_corrupt_generation_marker(tmp_path, monkeypatch):
    from services.health import _resolve_release_sha

    metadata = _release_metadata(tmp_path, monkeypatch)
    (metadata.parent / ".release-sha").write_text("b" * 40 + "\n", encoding="ascii")
    assert _resolve_release_sha() == (None, "release_generation_marker_invalid")


def test_release_identity_rehashes_all_manifested_content(tmp_path, monkeypatch):
    from services.health import _resolve_release_sha

    metadata = _release_metadata(tmp_path, monkeypatch)
    content = metadata.parent / "src/app.py"
    content.write_text("VALUE = 2\n", encoding="utf-8")
    content.chmod(0o644)
    assert _resolve_release_sha() == (None, "release_content_mismatch")


def test_release_identity_requires_explicit_metadata_path(monkeypatch):
    from services.health import _resolve_release_sha

    monkeypatch.delenv("BOT_RELEASE_METADATA_PATH", raising=False)
    monkeypatch.delenv("BOT_RELEASE_ROOT", raising=False)
    monkeypatch.setenv("BOT_RELEASE_METADATA_OWNER", "root")
    assert _resolve_release_sha() == (None, "release_metadata_path_unset")


def test_mem0_readiness_fails_closed_when_chroma_is_stopped(monkeypatch):
    from services.health import _check_mem0

    monkeypatch.setenv("CHROMA_MODE", "server")
    with patch("services.health.urlopen", side_effect=URLError("connection refused")):
        snapshot = _check_mem0()

    assert snapshot["ok"] is False
    assert snapshot["reason"] == "heartbeat_failed:URLError"


def test_mem0_readiness_rejects_non_heartbeat_200(monkeypatch):
    from services.health import _check_mem0

    response = MagicMock()
    response.status = 200
    response.read.return_value = b'{"status":"ok"}'
    response.__enter__.return_value = response
    monkeypatch.setenv("CHROMA_MODE", "server")
    with patch("services.health.urlopen", return_value=response):
        snapshot = _check_mem0()

    assert snapshot == {"ok": False, "reason": "invalid_heartbeat_payload"}

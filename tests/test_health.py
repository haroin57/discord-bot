"""Tests for services/health.py (SRE-C7)."""

import time
from unittest.mock import MagicMock, patch

import pytest

from services.health import get_health_snapshot, uptime_seconds


def test_uptime_increases_over_time():
    before = uptime_seconds()
    time.sleep(0.05)
    after = uptime_seconds()
    assert after > 0
    assert after >= before


def test_health_snapshot_keys():
    snapshot = get_health_snapshot(bot=None)
    assert "status" in snapshot
    assert "uptime_s" in snapshot
    assert "subsystems" in snapshot
    assert set(snapshot["subsystems"].keys()) == {"discord", "oauth", "mem0"}


def test_health_degraded_when_discord_not_ready():
    bot = MagicMock()
    bot.is_ready.return_value = False

    with patch("services.health._check_oauth", return_value={"ok": True}), \
         patch("services.health._check_mem0", return_value={"ok": True}):
        snapshot = get_health_snapshot(bot=bot)

    assert snapshot["status"] == "degraded"
    assert snapshot["subsystems"]["discord"]["ok"] is False


def test_health_ok_when_all_subsystems_ok():
    bot = MagicMock()
    bot.is_ready.return_value = True
    bot.latency = 0.042

    with patch("services.health._check_oauth", return_value={"ok": True}), \
         patch("services.health._check_mem0", return_value={"ok": True}):
        snapshot = get_health_snapshot(bot=bot)

    assert snapshot["status"] == "ok"
    assert snapshot["subsystems"]["discord"]["ok"] is True
    assert snapshot["subsystems"]["discord"]["gateway_latency_ms"] == 42

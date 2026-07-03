"""Tests for services/health_server.py — the minimal GET /health server.

Replaces the /health tests that lived in the deleted tests/test_web_api.py
(Phase 1-2). The external uptime Worker probes this endpoint every minute, so
the 200-when-ok / 503-when-degraded contract is load-bearing.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from services.health_server import HealthServer


def _make_server(bot):
    api = HealthServer(bot)
    # Register the route directly on the app, bypassing start() (which blocks
    # on asyncio.Event().wait()).
    api.app.router.add_get("/health", api._handle_health)
    return api


async def test_health_returns_200_and_ok_when_bot_ready():
    """GET /health -> 200 with status='ok' when every subsystem is healthy."""
    bot = MagicMock()
    bot.is_ready = MagicMock(return_value=True)
    bot.latency = 0.05  # real float for services.health._check_discord
    api = _make_server(bot)
    api._started_at = time.time() - 10

    with patch("services.health._check_oauth", return_value={"ok": True}), \
         patch("services.health._check_mem0", return_value={"ok": True}), \
         patch("services.health._check_system", return_value={"ok": True}):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["subsystems"]["discord"]["ok"] is True
            assert body["uptime_s"] >= 0
            # active_connections was Socket.IO-specific and is intentionally gone
            assert "active_connections" not in body


async def test_health_returns_503_when_degraded():
    """A degraded subsystem must surface as HTTP 503 so the Worker alerts."""
    bot = MagicMock()
    bot.is_ready = MagicMock(return_value=False)
    api = _make_server(bot)

    with patch("services.health._check_oauth", return_value={"ok": True}), \
         patch("services.health._check_mem0", return_value={"ok": True}), \
         patch("services.health._check_system", return_value={"ok": True}):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            body = await resp.json()
            assert body["status"] == "degraded"
            assert body["subsystems"]["discord"]["ok"] is False


async def test_health_falls_back_when_snapshot_raises():
    """If get_health_snapshot blows up, /health still answers (degraded)."""
    bot = MagicMock()
    bot.is_ready = MagicMock(return_value=False)
    api = _make_server(bot)

    with patch(
        "services.health.get_health_snapshot",
        side_effect=RuntimeError("boom"),
    ):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            body = await resp.json()
            assert body["status"] == "degraded"
            assert body["subsystems"]["discord"]["ok"] is False

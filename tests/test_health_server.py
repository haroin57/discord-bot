"""Tests for services/health_server.py — the minimal GET /health server.

Replaces the /health tests that lived in the deleted tests/test_web_api.py
(Phase 1-2). The external uptime Worker probes this endpoint every minute, so
the 200-when-ok / 503-when-degraded contract is load-bearing.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from services.health_server import HealthServer


def _make_server(bot):
    return HealthServer(bot)


async def test_health_returns_200_and_ok_when_bot_ready():
    """GET /health -> 200 with status='ok' when every subsystem is healthy."""
    bot = MagicMock()
    bot.is_ready = MagicMock(return_value=True)
    bot.latency = 0.05  # real float for services.health._check_discord
    api = _make_server(bot)
    api._started_at = time.time() - 10

    with (
        patch("services.health._check_oauth", return_value={"ok": True}),
        patch("services.health._check_mem0", return_value={"ok": True}),
        patch("services.health._check_system", return_value={"ok": True}),
        patch(
            "services.health._check_release",
            return_value={"ok": True, "sha": "a" * 40},
        ),
    ):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["subsystems"]["discord"]["ok"] is True
            assert body["uptime_s"] >= 0
            # active_connections was Socket.IO-specific and is intentionally gone
            assert "active_connections" not in body


async def test_live_returns_200_when_dependencies_are_not_ready():
    bot = MagicMock()
    bot.is_ready = MagicMock(return_value=False)
    api = _make_server(bot)

    with patch(
        "services.health.get_readiness_snapshot",
        side_effect=AssertionError("liveness must not probe readiness"),
    ):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/live")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "live"
            assert "subsystems" not in body


async def test_ready_returns_pending_until_release_verification_finishes():
    bot = MagicMock()
    bot.is_ready = MagicMock(return_value=True)
    bot.latency = 0.01
    api = _make_server(bot)

    with (
        patch("services.health._check_oauth", return_value={"ok": True}),
        patch("services.health._check_mem0", return_value={"ok": True}),
        patch("services.health._check_system", return_value={"ok": True}),
        patch("services.health._RELEASE_SHA", None),
        patch(
            "services.health._RELEASE_ERROR",
            "release_verification_pending",
        ),
    ):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/ready")
            body = await resp.json()

    assert resp.status == 503
    assert body["status"] == "degraded"
    assert body["subsystems"]["release"] == {
        "ok": False,
        "sha": None,
        "reason": "release_verification_pending",
    }


async def test_start_binds_before_background_release_verification():
    bot = MagicMock()
    api = _make_server(bot)
    bound = threading.Event()
    verification_started = threading.Event()
    release_verification = threading.Event()

    class FakeRunner:
        def __init__(self, app):
            self.app = app

        async def setup(self):
            return None

        async def cleanup(self):
            return None

    class FakeSite:
        def __init__(self, runner, host, port):
            self.runner = runner
            self.host = host
            self.port = port

        async def start(self):
            bound.set()

    def blocking_verification():
        assert bound.is_set()
        verification_started.set()
        assert release_verification.wait(timeout=2)
        return "a" * 40, None

    with (
        patch("services.health_server.web.AppRunner", FakeRunner),
        patch("services.health_server.web.TCPSite", FakeSite),
        patch(
            "services.health.initialize_release_verification",
            side_effect=blocking_verification,
        ),
    ):
        start_task = asyncio.create_task(api.start(port=3002))
        while not verification_started.is_set():
            await asyncio.sleep(0)

        live = await api._handle_live(MagicMock())
        assert live.status == 200
        assert not release_verification.is_set()

        release_verification.set()
        assert api._release_verification_task is not None
        await api._release_verification_task
        start_task.cancel()
        await start_task


async def test_health_returns_503_when_degraded():
    """A degraded subsystem must surface as HTTP 503 so the Worker alerts."""
    bot = MagicMock()
    bot.is_ready = MagicMock(return_value=False)
    api = _make_server(bot)

    with (
        patch("services.health._check_oauth", return_value={"ok": True}),
        patch("services.health._check_mem0", return_value={"ok": True}),
        patch("services.health._check_system", return_value={"ok": True}),
        patch(
            "services.health._check_release",
            return_value={"ok": True, "sha": "a" * 40},
        ),
    ):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            body = await resp.json()
            assert body["status"] == "degraded"
            assert body["subsystems"]["discord"]["ok"] is False


async def test_ready_snapshot_probe_does_not_block_event_loop():
    bot = MagicMock()
    api = _make_server(bot)
    probe_started = threading.Event()
    release_probe = threading.Event()

    def blocking_probe(*, bot):
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return {"status": "ok", "uptime_s": 1, "subsystems": {}}

    with patch("services.health.get_readiness_snapshot", side_effect=blocking_probe):
        async with TestClient(TestServer(api.app)) as client:
            request = asyncio.create_task(client.get("/ready"))
            while not probe_started.is_set():
                await asyncio.sleep(0)
            ticked = False

            async def tick():
                nonlocal ticked
                await asyncio.sleep(0)
                ticked = True

            await tick()
            assert ticked is True
            release_probe.set()
            response = await request
            assert response.status == 200


async def test_start_rejects_non_loopback_bind():
    api = _make_server(MagicMock())

    with pytest.raises(ValueError, match="restricted to 127.0.0.1"):
        await api.start(port=0, host="0.0.0.0")


async def test_ready_returns_503_when_degraded():
    bot = MagicMock()
    bot.is_ready = MagicMock(return_value=False)
    api = _make_server(bot)

    with (
        patch("services.health._check_oauth", return_value={"ok": True}),
        patch("services.health._check_mem0", return_value={"ok": True}),
        patch("services.health._check_system", return_value={"ok": True}),
        patch(
            "services.health._check_release",
            return_value={"ok": True, "sha": "a" * 40},
        ),
    ):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/ready")
            assert resp.status == 503
            body = await resp.json()
            assert body["status"] == "degraded"


async def test_health_falls_back_when_snapshot_raises():
    """If get_health_snapshot blows up, /health still answers (degraded)."""
    bot = MagicMock()
    # Discord alone being ready must not turn a failed aggregate probe green.
    bot.is_ready = MagicMock(return_value=True)
    api = _make_server(bot)

    with patch(
        "services.health.get_readiness_snapshot",
        side_effect=RuntimeError("boom"),
    ):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            body = await resp.json()
            assert body["status"] == "degraded"
            assert body["subsystems"]["readiness"]["ok"] is False

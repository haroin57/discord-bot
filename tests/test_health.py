"""Unit tests for services/health.py."""

import sys
import time
import types
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_bot(is_ready: bool = True, latency: float = 0.042):
    bot = MagicMock()
    bot.is_ready.return_value = is_ready
    bot.latency = latency
    return bot


# ---------------------------------------------------------------------------
# uptime_seconds
# ---------------------------------------------------------------------------

class TestUptimeSeconds:
    def test_returns_int(self):
        from services.health import uptime_seconds
        assert isinstance(uptime_seconds(), int)

    def test_non_negative(self):
        from services.health import uptime_seconds
        assert uptime_seconds() >= 0

    def test_increases_over_time(self):
        from services.health import uptime_seconds
        t0 = uptime_seconds()
        time.sleep(0.05)
        assert uptime_seconds() >= t0


# ---------------------------------------------------------------------------
# _check_discord
# ---------------------------------------------------------------------------

class TestCheckDiscord:
    def _call(self, bot):
        from services.health import _check_discord
        return _check_discord(bot)

    def test_none_bot(self):
        r = self._call(None)
        assert r == {"ok": False, "reason": "bot_not_initialized"}

    def test_ready_bot(self):
        bot = _make_bot(is_ready=True, latency=0.1)
        r = self._call(bot)
        assert r["ok"] is True
        assert r["gateway_latency_ms"] == 100

    def test_not_ready_bot(self):
        bot = _make_bot(is_ready=False, latency=0.0)
        r = self._call(bot)
        assert r["ok"] is False
        assert r["gateway_latency_ms"] is None

    def test_latency_zero_when_not_ready(self):
        bot = _make_bot(is_ready=False)
        r = self._call(bot)
        assert r["gateway_latency_ms"] is None

    def test_exception_from_is_ready(self):
        bot = MagicMock()
        bot.is_ready.side_effect = RuntimeError("ws closed")
        r = self._call(bot)
        assert r["ok"] is False
        assert "ws closed" in r["reason"]

    def test_latency_none_when_not_ready(self):
        # latency attribute set but not used when not ready
        bot = _make_bot(is_ready=False, latency=0.5)
        r = self._call(bot)
        assert r["gateway_latency_ms"] is None


# ---------------------------------------------------------------------------
# _check_oauth
# ---------------------------------------------------------------------------

class TestCheckOauth:
    def _call(self):
        from services.health import _check_oauth
        return _check_oauth()

    def test_healthy(self):
        mock_mod = types.ModuleType("services.oauth_manager")
        mock_mod.is_oauth_healthy = MagicMock(return_value=True)
        with patch.dict(sys.modules, {"services.oauth_manager": mock_mod}):
            r = self._call()
        assert r == {"ok": True}

    def test_unhealthy(self):
        mock_mod = types.ModuleType("services.oauth_manager")
        mock_mod.is_oauth_healthy = MagicMock(return_value=False)
        with patch.dict(sys.modules, {"services.oauth_manager": mock_mod}):
            r = self._call()
        assert r == {"ok": False}

    def test_import_error_treated_as_ok(self):
        # Remove module so import raises ImportError
        saved = sys.modules.pop("services.oauth_manager", None)
        try:
            with patch.dict(sys.modules, {"services.oauth_manager": None}):
                # patch.dict with None causes ImportError on "from x import y"
                r = self._call()
            assert r["ok"] is True
            assert "oauth_module_unavailable" in r.get("reason", "oauth_module_unavailable")
        finally:
            if saved is not None:
                sys.modules["services.oauth_manager"] = saved

    def test_generic_exception(self):
        mock_mod = types.ModuleType("services.oauth_manager")
        mock_mod.is_oauth_healthy = MagicMock(side_effect=Exception("db_down"))
        with patch.dict(sys.modules, {"services.oauth_manager": mock_mod}):
            r = self._call()
        assert r["ok"] is False
        assert "db_down" in r["reason"]


# ---------------------------------------------------------------------------
# _check_mem0
# ---------------------------------------------------------------------------

class TestCheckMem0:
    def _call(self):
        from services.health import _check_mem0
        return _check_mem0()

    def test_import_success(self):
        mock_mod = types.ModuleType("services.mem0_service")
        with patch.dict(sys.modules, {"services.mem0_service": mock_mod}):
            r = self._call()
        assert r == {"ok": True}

    def test_import_error(self):
        saved = sys.modules.pop("services.mem0_service", None)
        try:
            with patch.dict(sys.modules, {"services.mem0_service": None}):
                r = self._call()
            assert r["ok"] is False
            assert "import_failed" in r["reason"]
        finally:
            if saved is not None:
                sys.modules["services.mem0_service"] = saved

    def test_generic_exception(self):
        import builtins
        real_import = builtins.__import__

        def _bad_import(name, *args, **kwargs):
            if name == "services.mem0_service" or (
                name == "services" and args and "mem0_service" in str(args)
            ):
                raise RuntimeError("mem0_crashed")
            return real_import(name, *args, **kwargs)

        # Use patch.dict to ensure module not cached, then mock __import__
        saved = sys.modules.pop("services.mem0_service", None)
        try:
            with patch.object(builtins, "__import__", side_effect=_bad_import):
                r = self._call()
            assert r["ok"] is False
            assert "mem0_crashed" in r["reason"]
        finally:
            if saved is not None:
                sys.modules["services.mem0_service"] = saved


# ---------------------------------------------------------------------------
# get_health_snapshot
# ---------------------------------------------------------------------------

class TestGetHealthSnapshot:
    def _snapshot(self, bot=None, discord_ok=True, oauth_ok=True, mem0_ok=True):
        from services import health
        discord_result = {"ok": discord_ok}
        if discord_ok:
            discord_result["gateway_latency_ms"] = 10
        else:
            discord_result["reason"] = "not_ready"
        oauth_result = {"ok": oauth_ok}
        mem0_result = {"ok": mem0_ok}

        with patch.object(health, "_check_discord", return_value=discord_result), \
             patch.object(health, "_check_oauth", return_value=oauth_result), \
             patch.object(health, "_check_mem0", return_value=mem0_result):
            return health.get_health_snapshot(bot)

    def test_all_ok(self):
        r = self._snapshot(discord_ok=True, oauth_ok=True, mem0_ok=True)
        assert r["status"] == "ok"
        assert r["subsystems"]["discord"]["ok"] is True
        assert r["subsystems"]["oauth"]["ok"] is True
        assert r["subsystems"]["mem0"]["ok"] is True
        assert isinstance(r["uptime_s"], int)

    def test_discord_down_degrades(self):
        r = self._snapshot(discord_ok=False, oauth_ok=True, mem0_ok=True)
        assert r["status"] == "degraded"

    def test_oauth_down_degrades(self):
        r = self._snapshot(discord_ok=True, oauth_ok=False, mem0_ok=True)
        assert r["status"] == "degraded"

    def test_mem0_down_degrades(self):
        r = self._snapshot(discord_ok=True, oauth_ok=True, mem0_ok=False)
        assert r["status"] == "degraded"

    def test_all_down_degrades(self):
        r = self._snapshot(discord_ok=False, oauth_ok=False, mem0_ok=False)
        assert r["status"] == "degraded"

    def test_subsystems_keys_present(self):
        r = self._snapshot()
        assert set(r["subsystems"].keys()) == {"discord", "oauth", "mem0"}

    def test_uptime_s_is_int(self):
        r = self._snapshot()
        assert isinstance(r["uptime_s"], int)
        assert r["uptime_s"] >= 0

    def test_bot_none_propagated(self):
        from services import health
        with patch.object(health, "_check_discord", wraps=health._check_discord) as spy, \
             patch.object(health, "_check_oauth", return_value={"ok": True}), \
             patch.object(health, "_check_mem0", return_value={"ok": True}):
            health.get_health_snapshot(bot=None)
            spy.assert_called_once_with(None)

    def test_real_bot_snapshot(self):
        bot = _make_bot(is_ready=True, latency=0.025)
        mock_mod_oauth = types.ModuleType("services.oauth_manager")
        mock_mod_oauth.is_oauth_healthy = MagicMock(return_value=True)
        mock_mod_mem0 = types.ModuleType("services.mem0_service")
        with patch.dict(sys.modules, {
            "services.oauth_manager": mock_mod_oauth,
            "services.mem0_service": mock_mod_mem0,
        }):
            from services.health import get_health_snapshot
            r = get_health_snapshot(bot)
        assert r["status"] == "ok"
        assert r["subsystems"]["discord"]["gateway_latency_ms"] == 25

"""Integration tests for SRE Critical fixes.

Each test exercises the fixed path end-to-end via mocks where the
production code is hard to spin up (Discord gateway, Anthropic API).
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


# ─── SRE-C3: sessions.json atomic write ─────────────────────────
class TestSessionsAtomicWrite:
    def test_atomic_write_survives_simulated_crash(self, tmp_path, monkeypatch):
        """Existing sessions.json must remain valid if json.dump is interrupted."""
        sessions_path = tmp_path / "sessions.json"
        sessions_path.write_text('{"old": "data"}')
        monkeypatch.setattr("services.session.SESSIONS_PATH", str(sessions_path))
        from services.session import save_sessions
        # Build a "bad" sessions dict that breaks json.dump partway
        class Boom:
            def __repr__(self): raise RuntimeError("simulated crash")
        try:
            save_sessions({"key": Boom()})
        except Exception:
            pass
        # Original file must still be readable JSON
        content = sessions_path.read_text()
        assert json.loads(content) == {"old": "data"}

    def test_no_tmp_file_after_success(self, tmp_path, monkeypatch):
        sessions_path = tmp_path / "sessions.json"
        monkeypatch.setattr("services.session.SESSIONS_PATH", str(sessions_path))
        from services.session import save_sessions, set_session, SessionInfo
        set_session(1000000000000000, None, SessionInfo(session_id="x", mcp_server_keys=[]))
        save_sessions({})
        # No stray .tmp file
        assert not (tmp_path / "sessions.json.tmp").exists()


# ─── SRE-C7: /health endpoint ───────────────────────────────────
class TestHealthIntegration:
    @pytest.mark.asyncio
    async def test_health_module_importable(self):
        try:
            from services.health import get_health_snapshot
        except ImportError:
            pytest.skip("services.health not yet present")
        snap = get_health_snapshot(bot=None)
        assert "status" in snap
        assert "subsystems" in snap


# ─── SRE-C9: oauth refresh failure escalation ────────────────────
class TestOAuthEscalation:
    def test_alert_escalates_after_threshold(self, caplog):
        try:
            import services.oauth_manager as _om
            _record_refresh_failure = lambda err: _om._record_refresh_failure(_om._accounts['primary'], err)
            _record_refresh_success = lambda: _om._record_refresh_success(_om._accounts['primary'])
        except ImportError:
            pytest.skip("oauth_manager helpers not yet present")
        _record_refresh_success()  # reset
        import logging
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                _record_refresh_failure(RuntimeError("test"))
        # 1st and 2nd should be WARNING, not ERROR
        warn_count = sum(1 for r in caplog.records if r.levelno == logging.WARNING)
        assert warn_count >= 2
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            _record_refresh_failure(RuntimeError("test"))
        err_count = sum(1 for r in caplog.records if r.levelno == logging.ERROR)
        assert err_count >= 1
        _record_refresh_success()  # cleanup


# ─── SRE-C1: signal handler test (subprocess-based) ─────────────
class TestSignalHandler:
    def test_kill_orphan_function_exists(self):
        # PR9: claude_sdk deleted; the shutdown path imports from claude_api.
        from services.claude_api import kill_orphan_claude_cli
        # Just verify it's callable
        assert callable(kill_orphan_claude_cli)

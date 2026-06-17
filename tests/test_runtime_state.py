"""Tests for services/runtime_state.py (機能2 — cross-restart persistence)."""
import json
import time

import pytest

import services.runtime_state as rs
from services import rate_limit as rl
from services.claude_api import session_store as ss


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point persistence at a temp file and clear all ledgers between tests."""
    path = str(tmp_path / "runtime_state.json")
    monkeypatch.setattr(rs, "RUNTIME_STATE_PATH", path)
    rl.reset_for_tests()
    ss._session_timestamps.clear()
    ss._session_sticky_model.clear()
    yield path
    rl.reset_for_tests()
    ss._session_timestamps.clear()
    ss._session_sticky_model.clear()


class TestRoundTrip:
    def test_save_then_load_restores_all_providers(self, _isolate):
        rl.check_cooldown("u1", is_admin=False)
        rl.check_daily_quota("u1", is_admin=False)
        ss._record_session_time(10, 20)
        ss._record_sticky_model(10, "claude-opus-4-8", 20)

        rs.save_all_sync()
        assert json.load(open(_isolate))  # file written, non-empty

        # Wipe in-memory, then restore from disk.
        rl.reset_for_tests()
        ss._session_timestamps.clear()
        ss._session_sticky_model.clear()
        rs.load_all()

        # Cooldown still blocks; quota counter preserved; session restored.
        assert rl.check_cooldown("u1", is_admin=False)[0] is False
        assert ss._is_session_fresh(10, 20) is True
        assert ss._get_sticky_model(10, 20) == "claude-opus-4-8"

    def test_composite_key_survives_round_trip(self, _isolate):
        # Two users in the same channel must stay separate across a restart.
        ss._record_session_time(100, 1)
        ss._record_session_time(100, 2)
        rs.save_all_sync()
        ss._session_timestamps.clear()
        rs.load_all()
        assert ss._is_session_fresh(100, 1) is True
        assert ss._is_session_fresh(100, 2) is True
        assert ss._is_session_fresh(100, 999) is False  # never recorded


class TestTTLExpiry:
    def test_stale_session_timestamp_dropped_on_load(self, _isolate, monkeypatch):
        # A timestamp older than SESSION_TTL_S at load time must be discarded,
        # so the user starts fresh after a long downtime.
        from config import SESSION_TTL_S
        now = [1_000_000.0]
        monkeypatch.setattr(time, "time", lambda: now[0])
        ss._record_session_time(7, None)
        rs.save_all_sync()
        ss._session_timestamps.clear()
        # Advance the wall clock past the TTL, then reload.
        now[0] += SESSION_TTL_S + 10
        rs.load_all()
        assert ss._is_session_fresh(7, None) is False
        assert (7, None) not in ss._session_timestamps

    def test_expired_cooldown_dropped_on_load(self, _isolate, monkeypatch):
        from config import COOLDOWN_SECONDS, RESTRICTED_COOLDOWN_SECONDS
        max_cd = max(COOLDOWN_SECONDS, RESTRICTED_COOLDOWN_SECONDS)
        now = [1_000_000.0]
        monkeypatch.setattr(time, "time", lambda: now[0])
        rl.check_cooldown("old", is_admin=False)
        rs.save_all_sync()
        rl.reset_for_tests()
        now[0] += max_cd + 10
        rs.load_all()
        # The stale cooldown was dropped → user can send again immediately.
        assert rl.check_cooldown("old", is_admin=False)[0] is True


class TestLoadMissing:
    def test_load_no_file_is_noop(self, _isolate):
        # Fresh deploy: no state file yet. load_all must not raise.
        rs.load_all()
        assert ss._session_timestamps == {}

    def test_load_corrupt_json_is_noop(self, _isolate):
        with open(_isolate, "w") as f:
            f.write("{ not json")
        rs.load_all()  # must swallow JSONDecodeError
        assert ss._session_timestamps == {}

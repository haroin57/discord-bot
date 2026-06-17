"""Tests for services.claude_api.session_store — freshness TTL tracking."""

import time

import pytest

from services.claude_api import session_store as ss
from services.claude_api.session_store import (
    SESSION_TTL_S,
    _clear_session_time,
    _get_sticky_model,
    _is_session_fresh,
    _record_session_time,
    _record_sticky_model,
)


@pytest.fixture(autouse=True)
def clean_session_timestamps():
    ss._session_timestamps.clear()
    ss._session_sticky_model.clear()
    yield
    ss._session_timestamps.clear()
    ss._session_sticky_model.clear()


class TestSessionFreshness:
    def test_unknown_channel_is_not_fresh(self):
        assert _is_session_fresh(42) is False

    def test_just_recorded_is_fresh(self):
        _record_session_time(42)
        assert _is_session_fresh(42) is True

    def test_after_ttl_is_not_fresh(self, monkeypatch):
        now = [1000.0]
        # 機能2: freshness now uses wall-clock (time.time), not monotonic, so
        # it can be persisted across restarts. Patch time.time accordingly.
        monkeypatch.setattr(time, "time", lambda: now[0])
        _record_session_time(42)
        now[0] += SESSION_TTL_S + 1
        assert _is_session_fresh(42) is False

    def test_clear_removes_entry(self):
        _record_session_time(42)
        assert _is_session_fresh(42) is True
        _clear_session_time(42)
        assert _is_session_fresh(42) is False

    def test_clear_unknown_channel_is_noop(self):
        _clear_session_time(9999)  # must not raise


class TestStickyModel:
    """ADR-0010: refusal-fallback model pinning per session."""

    def test_unknown_channel_returns_none(self):
        assert _get_sticky_model(42) is None

    def test_recorded_and_fresh_returns_model(self):
        _record_session_time(42)
        _record_sticky_model(42, "claude-opus-4-8")
        assert _get_sticky_model(42) == "claude-opus-4-8"

    def test_recorded_but_never_fresh_returns_none(self):
        # Sticky model without a freshness timestamp must not leak through:
        # a stale/new session always retries the default model first.
        _record_sticky_model(42, "claude-opus-4-8")
        assert _get_sticky_model(42) is None

    def test_ttl_lapse_resets_to_none(self, monkeypatch):
        now = [1000.0]
        # 機能2: freshness now uses wall-clock (time.time), not monotonic, so
        # it can be persisted across restarts. Patch time.time accordingly.
        monkeypatch.setattr(time, "time", lambda: now[0])
        _record_session_time(42)
        _record_sticky_model(42, "claude-opus-4-8")
        assert _get_sticky_model(42) == "claude-opus-4-8"
        now[0] += SESSION_TTL_S + 1
        assert _get_sticky_model(42) is None

    def test_clear_session_also_clears_sticky(self):
        _record_session_time(42)
        _record_sticky_model(42, "claude-opus-4-8")
        _clear_session_time(42)
        # Even if a new session starts right after, the old pin must be gone.
        _record_session_time(42)
        assert _get_sticky_model(42) is None

    def test_pin_is_per_channel(self):
        _record_session_time(1)
        _record_session_time(2)
        _record_sticky_model(1, "claude-opus-4-8")
        assert _get_sticky_model(1) == "claude-opus-4-8"
        assert _get_sticky_model(2) is None

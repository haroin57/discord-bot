"""Tests for services.claude_api.session_store — freshness TTL tracking."""

import time

import pytest

from services.claude_api import session_store as ss
from services.claude_api.session_store import (
    SESSION_TTL_S,
    _clear_session_time,
    _is_session_fresh,
    _record_session_time,
)


@pytest.fixture(autouse=True)
def clean_session_timestamps():
    ss._session_timestamps.clear()
    yield
    ss._session_timestamps.clear()


class TestSessionFreshness:
    def test_unknown_channel_is_not_fresh(self):
        assert _is_session_fresh(42) is False

    def test_just_recorded_is_fresh(self):
        _record_session_time(42)
        assert _is_session_fresh(42) is True

    def test_after_ttl_is_not_fresh(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: now[0])
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

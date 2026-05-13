"""Tests for services.claude_api.retry — per-channel budget + backoff."""

import time

import pytest

from services.claude_api import retry as retry_mod
from services.claude_api.retry import (
    BASE_BACKOFF_S,
    MAX_BACKOFF_S,
    _RETRY_BUDGET_MAX_CHANNELS,
    _RETRY_BUDGET_PER_WINDOW,
    _RETRY_BUDGET_WINDOW_S,
    check_and_record_retry_budget,
    compute_backoff,
)


@pytest.fixture(autouse=True)
def clean_retry_budget():
    retry_mod._retry_budget.clear()
    yield
    retry_mod._retry_budget.clear()


class TestRetryBudget:
    def test_first_retry_permitted(self):
        assert check_and_record_retry_budget(1) is True
        assert len(retry_mod._retry_budget[1]) == 1

    def test_under_limit_permitted(self):
        for _ in range(_RETRY_BUDGET_PER_WINDOW):
            assert check_and_record_retry_budget(1) is True

    def test_over_limit_denied(self):
        for _ in range(_RETRY_BUDGET_PER_WINDOW):
            check_and_record_retry_budget(1)
        assert check_and_record_retry_budget(1) is False

    def test_stale_entries_purged(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: now[0])
        # Fill the budget at t=0
        for _ in range(_RETRY_BUDGET_PER_WINDOW):
            check_and_record_retry_budget(1)
        # Jump past the window
        now[0] += _RETRY_BUDGET_WINDOW_S + 1
        assert check_and_record_retry_budget(1) is True

    def test_per_channel_isolation(self):
        for _ in range(_RETRY_BUDGET_PER_WINDOW):
            check_and_record_retry_budget(1)
        # Channel 2 is unaffected
        assert check_and_record_retry_budget(2) is True

    def test_fifo_eviction_at_capacity(self):
        # Fill to capacity with distinct channels
        for cid in range(_RETRY_BUDGET_MAX_CHANNELS):
            check_and_record_retry_budget(cid)
        assert len(retry_mod._retry_budget) == _RETRY_BUDGET_MAX_CHANNELS
        # One more channel → oldest (channel 0) evicted FIFO
        check_and_record_retry_budget(99999)
        assert 0 not in retry_mod._retry_budget
        assert 99999 in retry_mod._retry_budget


class TestComputeBackoff:
    def test_grows_exponentially(self):
        assert compute_backoff(0) == BASE_BACKOFF_S
        assert compute_backoff(1) == BASE_BACKOFF_S * 2
        assert compute_backoff(2) == BASE_BACKOFF_S * 4

    def test_capped_at_max(self):
        assert compute_backoff(100) == MAX_BACKOFF_S

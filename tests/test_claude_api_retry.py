"""Tests for services.claude_api.retry — per-channel budget + backoff."""

import time

import pytest

from services.claude_api import retry as retry_mod
from services.claude_api.retry import (
    BASE_BACKOFF_S,
    MAX_BACKOFF_S,
    TRANSIENT_MAX_ATTEMPTS,
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
    def test_grows_exponentially_without_jitter(self):
        assert compute_backoff(0, jitter=False) == BASE_BACKOFF_S
        assert compute_backoff(1, jitter=False) == BASE_BACKOFF_S * 2
        assert compute_backoff(2, jitter=False) == BASE_BACKOFF_S * 4

    def test_capped_at_max_without_jitter(self):
        assert compute_backoff(100, jitter=False) == MAX_BACKOFF_S

    def test_jitter_stays_in_equal_jitter_band(self):
        # equal jitter: result in [capped/2, capped] for every attempt.
        for attempt in range(6):
            capped = min(BASE_BACKOFF_S * (2 ** attempt), MAX_BACKOFF_S)
            for _ in range(200):
                delay = compute_backoff(attempt)  # jitter=True default
                assert capped / 2 <= delay <= capped

    def test_jitter_capped_at_max(self):
        for _ in range(200):
            assert compute_backoff(100) <= MAX_BACKOFF_S

    def test_jitter_actually_varies(self):
        # With jitter on, repeated calls must not all be identical.
        samples = {compute_backoff(3) for _ in range(50)}
        assert len(samples) > 1


class TestTransientRetryBound:
    """ADR-0006: the transient retry loop runs range(TRANSIENT_MAX_ATTEMPTS),
    so the final attempt index is TRANSIENT_MAX_ATTEMPTS - 1. entry.py uses
    `attempt >= TRANSIENT_MAX_ATTEMPTS - 1` as the exhaustion check. Guard the
    invariant that this check fires exactly once, on the last attempt — the
    off-by-one regression made it never fire, so overloaded fell through to the
    "unknown" fallback ("内部エラー")."""

    def test_exhaustion_check_fires_on_last_attempt(self):
        attempts = list(range(TRANSIENT_MAX_ATTEMPTS))
        exhausted = [a for a in attempts if a >= TRANSIENT_MAX_ATTEMPTS - 1]
        # Exactly the final attempt triggers exhaustion — never zero (the bug).
        assert exhausted == [TRANSIENT_MAX_ATTEMPTS - 1]

    def test_loop_range_reaches_exhaustion(self):
        # The last value yielded by the loop must satisfy the exhaustion check,
        # otherwise the loop falls through with result=None.
        last_attempt = list(range(TRANSIENT_MAX_ATTEMPTS))[-1]
        assert last_attempt >= TRANSIENT_MAX_ATTEMPTS - 1

"""Unit tests for services/claude_api/ratelimit.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_ratelimit_state():
    """Reset the module-level singleton between every test."""
    from services.claude_api import ratelimit

    ratelimit._state = ratelimit.RateLimitState()
    yield
    ratelimit._state = ratelimit.RateLimitState()


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------
class TestObserve:
    def test_observe_updates_state(self):
        from services.claude_api import ratelimit

        headers = {
            "anthropic-ratelimit-unified-status": "allowed_warning",
            "anthropic-ratelimit-unified-representative-claim": "five_hour",
            "anthropic-ratelimit-unified-5h-status": "allowed_warning",
            "anthropic-ratelimit-unified-5h-utilization": "0.85",
            "anthropic-ratelimit-unified-5h-reset": "1700000000",
            "anthropic-ratelimit-unified-7d-status": "allowed",
            "anthropic-ratelimit-unified-7d-utilization": "0.40",
            "anthropic-ratelimit-unified-7d-reset": "1700100000",
            "anthropic-ratelimit-unified-7d-surpassed-threshold": "0.75",
            "anthropic-ratelimit-unified-fallback-percentage": "0.10",
        }

        state = ratelimit.observe(headers)

        assert state.status == "allowed_warning"
        assert state.representative == "five_hour"
        assert state.five_h_status == "allowed_warning"
        assert abs(state.five_h_utilization - 0.85) < 1e-9
        assert state.five_h_reset == 1700000000
        assert state.seven_d_status == "allowed"
        assert abs(state.seven_d_utilization - 0.40) < 1e-9
        assert state.seven_d_reset == 1700100000
        assert abs(state.seven_d_surpassed - 0.75) < 1e-9
        assert abs(state.fallback_percentage - 0.10) < 1e-9
        assert state.last_observed > 0

    def test_observe_case_insensitive(self):
        """observe() normalises keys to lowercase before reading them."""
        from services.claude_api import ratelimit

        # Send headers with mixed case (e.g. from a transport that doesn't
        # lower-case automatically)
        headers = {
            "Anthropic-RateLimit-Unified-Status": "allowed_warning",
            "ANTHROPIC-RATELIMIT-UNIFIED-FALLBACK-PERCENTAGE": "0.50",
        }
        state = ratelimit.observe(headers)

        assert state.status == "allowed_warning"
        assert abs(state.fallback_percentage - 0.50) < 1e-9

    def test_observe_non_dict_returns_state(self):
        """A non-iterable input must not crash; the existing state is returned."""
        from services.claude_api import ratelimit

        original_status = ratelimit._state.status
        state = ratelimit.observe(None)  # AttributeError path
        assert state is ratelimit._state
        assert state.status == original_status

    def test_observe_missing_headers_leave_defaults(self):
        """Headers not present in the dict don't overwrite existing state."""
        from services.claude_api import ratelimit

        ratelimit._state.status = "allowed_warning"
        ratelimit.observe({})  # no relevant keys
        assert ratelimit._state.status == "allowed_warning"

    def test_observe_partial_update(self):
        """Only the keys present in the dict are updated."""
        from services.claude_api import ratelimit

        ratelimit._state.fallback_percentage = 0.5
        ratelimit.observe({"anthropic-ratelimit-unified-status": "rejected"})
        assert ratelimit._state.status == "rejected"
        assert abs(ratelimit._state.fallback_percentage - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------
class TestGetState:
    def test_get_state_returns_module_singleton(self):
        from services.claude_api import ratelimit

        assert ratelimit.get_state() is ratelimit._state


# ---------------------------------------------------------------------------
# should_block
# ---------------------------------------------------------------------------
class TestShouldBlock:
    def test_should_block_when_fallback_high(self):
        """fallback_percentage >= 0.99 triggers a block."""
        from services.claude_api import ratelimit

        ratelimit._state.fallback_percentage = 0.99
        blocked, reason = ratelimit.should_block()
        assert blocked is True
        assert reason  # non-empty explanation

    def test_should_block_exact_threshold(self):
        from services.claude_api import ratelimit

        ratelimit._state.fallback_percentage = 1.0
        blocked, _ = ratelimit.should_block()
        assert blocked is True

    def test_should_block_when_allowed(self):
        """fallback_percentage < 0.99 must not block."""
        from services.claude_api import ratelimit

        ratelimit._state.fallback_percentage = 0.98
        blocked, reason = ratelimit.should_block()
        assert blocked is False
        assert reason == ""

    def test_should_block_default_state(self):
        """Fresh (default) state never blocks."""
        from services.claude_api import ratelimit

        blocked, _ = ratelimit.should_block()
        assert blocked is False

    def test_should_block_rejected_status_does_not_block(self):
        """status=rejected alone does not pre-emptively block (overage path)."""
        from services.claude_api import ratelimit

        ratelimit._state.status = "rejected"
        ratelimit._state.fallback_percentage = 0.0
        blocked, _ = ratelimit.should_block()
        assert blocked is False


# ---------------------------------------------------------------------------
# maybe_wait_before_request
# ---------------------------------------------------------------------------
class TestMaybeWaitBeforeRequest:
    async def test_maybe_wait_no_warning(self):
        """Default state (status=allowed, fallback=0) → no sleep."""
        from services.claude_api import ratelimit

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await ratelimit.maybe_wait_before_request()

        mock_sleep.assert_not_called()

    async def test_maybe_wait_warning_sleeps(self):
        """status=allowed_warning with a recent prior request → sleep ~3 s."""
        import time

        from services.claude_api import ratelimit

        # Simulate a request that happened 0 s ago (last_request_at = now)
        ratelimit._state.status = "allowed_warning"
        ratelimit._state.last_request_at = time.time()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await ratelimit.maybe_wait_before_request()

        mock_sleep.assert_awaited_once()
        sleep_arg = mock_sleep.await_args.args[0]
        # Should be close to _GAP_WHEN_WARNING_S (3.0 s)
        assert 0 < sleep_arg <= ratelimit._GAP_WHEN_WARNING_S

    async def test_maybe_wait_fallback_sleeps(self):
        """fallback_percentage > 0 → sleep up to _GAP_WHEN_HARD_S (10 s)."""
        import time

        from services.claude_api import ratelimit

        ratelimit._state.fallback_percentage = 0.5
        ratelimit._state.last_request_at = time.time()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await ratelimit.maybe_wait_before_request()

        mock_sleep.assert_awaited_once()
        sleep_arg = mock_sleep.await_args.args[0]
        assert 0 < sleep_arg <= ratelimit._GAP_WHEN_HARD_S

    async def test_maybe_wait_elapsed_gap_no_sleep(self):
        """If enough time has already elapsed since last_request_at, no sleep."""
        import time

        from services.claude_api import ratelimit

        ratelimit._state.status = "allowed_warning"
        # Set last_request_at far enough in the past that the gap is exceeded
        ratelimit._state.last_request_at = time.time() - ratelimit._GAP_WHEN_WARNING_S - 1

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await ratelimit.maybe_wait_before_request()

        mock_sleep.assert_not_called()

    async def test_maybe_wait_updates_last_request_at(self):
        """After returning, last_request_at is refreshed regardless of sleep."""
        import time

        from services.claude_api import ratelimit

        before = time.time()
        ratelimit._state.last_request_at = 0.0

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await ratelimit.maybe_wait_before_request()

        assert ratelimit._state.last_request_at >= before

    async def test_maybe_wait_surpassed_threshold_sleeps(self):
        """seven_d_utilization >= seven_d_surpassed > 0 also triggers the gap."""
        import time

        from services.claude_api import ratelimit

        ratelimit._state.seven_d_utilization = 0.80
        ratelimit._state.seven_d_surpassed = 0.75
        ratelimit._state.last_request_at = time.time()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await ratelimit.maybe_wait_before_request()

        mock_sleep.assert_awaited_once()
        sleep_arg = mock_sleep.await_args.args[0]
        assert 0 < sleep_arg <= ratelimit._GAP_WHEN_WARNING_S


# ---------------------------------------------------------------------------
# Safe type parsers
# ---------------------------------------------------------------------------
class TestSafeParsers:
    def test_float_or_valid(self):
        from services.claude_api.ratelimit import _float_or

        assert _float_or({"k": "0.75"}, "k") == pytest.approx(0.75)

    def test_float_or_invalid(self):
        from services.claude_api.ratelimit import _float_or

        assert _float_or({"k": "not_a_float"}, "k", default=99.0) == 99.0

    def test_float_or_missing(self):
        from services.claude_api.ratelimit import _float_or

        assert _float_or({}, "missing", default=3.14) == pytest.approx(3.14)

    def test_int_or_valid(self):
        from services.claude_api.ratelimit import _int_or

        assert _int_or({"k": "42"}, "k") == 42

    def test_int_or_invalid(self):
        from services.claude_api.ratelimit import _int_or

        assert _int_or({"k": "bad"}, "k", default=7) == 7

    def test_int_or_missing(self):
        from services.claude_api.ratelimit import _int_or

        assert _int_or({}, "missing", default=0) == 0

    def test_str_or_valid(self):
        from services.claude_api.ratelimit import _str_or

        assert _str_or({"k": "hello"}, "k") == "hello"

    def test_str_or_non_string_value_returns_default(self):
        """If the header value is not a str, the default is returned."""
        from services.claude_api.ratelimit import _str_or

        assert _str_or({"k": 123}, "k", default="fallback") == "fallback"

    def test_str_or_missing(self):
        from services.claude_api.ratelimit import _str_or

        assert _str_or({}, "missing", default="x") == "x"

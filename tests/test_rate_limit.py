import time
import unittest.mock as mock
import pytest
import services.rate_limit as rl
from services.rate_limit import (
    check_cooldown, check_daily_quota, reset_for_tests,
    _cooldowns, _daily_quota,
)
from config import (
    COOLDOWN_SECONDS,
    RESTRICTED_COOLDOWN_SECONDS,
    RESTRICTED_DAILY_QUOTA,
    MAX_COOLDOWN_ENTRIES,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


# --- check_cooldown ---

def test_first_cooldown_allowed():
    allowed, wait = check_cooldown("u1", is_admin=False)
    assert allowed is True and wait == 0


def test_second_cooldown_blocked():
    check_cooldown("u1", is_admin=False)
    allowed, wait = check_cooldown("u1", is_admin=False)
    assert allowed is False and wait > 0


def test_admin_uses_shorter_cooldown():
    """Admin cooldown (COOLDOWN_SECONDS) is shorter than restricted."""
    check_cooldown("admin1", is_admin=True)
    # Advance time by admin cooldown but not restricted cooldown
    with mock.patch("services.rate_limit.time") as mt:
        mt.time.return_value = time.time() + COOLDOWN_SECONDS + 1
        allowed, wait = check_cooldown("admin1", is_admin=True)
    assert allowed is True and wait == 0


def test_non_admin_blocked_within_restricted_cooldown():
    """Non-admin must wait RESTRICTED_COOLDOWN_SECONDS."""
    check_cooldown("na1", is_admin=False)
    # Advance by less than restricted cooldown
    with mock.patch("services.rate_limit.time") as mt:
        mt.time.return_value = time.time() + RESTRICTED_COOLDOWN_SECONDS - 2
        allowed, wait = check_cooldown("na1", is_admin=False)
    assert allowed is False and wait >= 1


def test_cooldown_wait_value():
    """wait_seconds is approximately effective cooldown."""
    check_cooldown("u2", is_admin=False)
    _, wait = check_cooldown("u2", is_admin=False)
    assert 1 <= wait <= RESTRICTED_COOLDOWN_SECONDS


def test_cooldown_max_entries_eviction():
    """Oldest entry is evicted when MAX_COOLDOWN_ENTRIES is exceeded."""
    for i in range(MAX_COOLDOWN_ENTRIES + 1):
        check_cooldown(f"evict_{i}", is_admin=False)
    assert "evict_0" not in _cooldowns
    assert len(_cooldowns) == MAX_COOLDOWN_ENTRIES


def test_cooldown_independent_users():
    check_cooldown("a", is_admin=False)
    allowed, _ = check_cooldown("b", is_admin=False)
    assert allowed is True


# --- check_daily_quota ---

def test_admin_unlimited_quota():
    allowed, remaining = check_daily_quota("admin", is_admin=True)
    assert allowed is True and remaining == -1


def test_quota_shared_state_singleton():
    check_daily_quota("user_x", is_admin=False)
    check_daily_quota("user_x", is_admin=False)
    _, rem = check_daily_quota("user_x", is_admin=False)
    assert rem == RESTRICTED_DAILY_QUOTA - 3


def test_quota_exhausted_returns_false():
    """After RESTRICTED_DAILY_QUOTA uses, returns (False, 0)."""
    for _ in range(RESTRICTED_DAILY_QUOTA):
        check_daily_quota("q1", is_admin=False)
    allowed, remaining = check_daily_quota("q1", is_admin=False)
    assert allowed is False and remaining == 0


def test_quota_exhausted_bucket_preserved():
    """Exhausted bucket size stays at quota, not growing."""
    for _ in range(RESTRICTED_DAILY_QUOTA + 2):
        check_daily_quota("q2", is_admin=False)
    assert len(_daily_quota.get("q2", [])) == RESTRICTED_DAILY_QUOTA


def test_quota_zero_via_mock():
    """RESTRICTED_DAILY_QUOTA <= 0 makes non-admin unlimited."""
    with mock.patch("services.rate_limit.RESTRICTED_DAILY_QUOTA", 0):
        allowed, remaining = check_daily_quota("qz", is_admin=False)
    assert allowed is True and remaining == -1


def test_quota_timestamp_expiry():
    """Timestamps older than 24 h are evicted and quota resets."""
    old_time = time.time() - (25 * 3600)
    _daily_quota["exp1"] = [old_time] * RESTRICTED_DAILY_QUOTA
    # Bucket is stale — should be allowed again
    allowed, _ = check_daily_quota("exp1", is_admin=False)
    assert allowed is True


def test_quota_max_entries_eviction():
    """Oldest daily-quota entry evicted when MAX_COOLDOWN_ENTRIES exceeded."""
    for i in range(MAX_COOLDOWN_ENTRIES + 1):
        check_daily_quota(f"dq_{i}", is_admin=False)
    assert "dq_0" not in _daily_quota
    assert len(_daily_quota) == MAX_COOLDOWN_ENTRIES

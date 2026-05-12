import time
import pytest
from services.rate_limit import (
    check_cooldown, check_daily_quota, reset_for_tests,
    _cooldowns, _daily_quota,
)

@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()

def test_first_cooldown_allowed():
    allowed, wait = check_cooldown("u1", is_admin=False)
    assert allowed is True and wait == 0

def test_second_cooldown_blocked():
    check_cooldown("u1", is_admin=False)
    allowed, wait = check_cooldown("u1", is_admin=False)
    assert allowed is False and wait > 0

def test_admin_unlimited_quota():
    allowed, remaining = check_daily_quota("admin", is_admin=True)
    assert allowed is True

def test_quota_shared_state_singleton():
    # Two callers consume quota; the third sees reduced remaining
    check_daily_quota("user_x", is_admin=False)
    check_daily_quota("user_x", is_admin=False)
    _, rem = check_daily_quota("user_x", is_admin=False)
    from config import RESTRICTED_DAILY_QUOTA
    assert rem == RESTRICTED_DAILY_QUOTA - 3

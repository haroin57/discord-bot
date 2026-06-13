"""Shared rate-limit ledger used by both Discord bot and Web API.

Previously bot.py and web_api.py kept independent counters, allowing a
non-admin to consume RESTRICTED_DAILY_QUOTA on each surface for a 2x
effective quota. This module is the single source of truth.
"""
import logging
import time
from collections import OrderedDict
from threading import Lock

from config import (
    COOLDOWN_SECONDS,
    MAX_COOLDOWN_ENTRIES,
    RESTRICTED_COOLDOWN_SECONDS,
    RESTRICTED_DAILY_QUOTA,
)

log = logging.getLogger(__name__)

_QUOTA_WINDOW_S = 24 * 60 * 60
_lock = Lock()
_cooldowns: OrderedDict[str, float] = OrderedDict()
_daily_quota: OrderedDict[str, list[float]] = OrderedDict()


def check_cooldown(discord_id: str, is_admin: bool) -> tuple[bool, int]:
    """Returns (allowed, wait_seconds). Updates shared cooldown ledger on success."""
    with _lock:
        now = time.time()
        last = _cooldowns.get(discord_id, 0)
        effective = COOLDOWN_SECONDS if is_admin else RESTRICTED_COOLDOWN_SECONDS
        if now - last < effective:
            return False, int(effective - (now - last))
        _cooldowns[discord_id] = now
        _cooldowns.move_to_end(discord_id)
        while len(_cooldowns) > MAX_COOLDOWN_ENTRIES:
            _cooldowns.popitem(last=False)
        return True, 0


def check_daily_quota(discord_id: str, is_admin: bool) -> tuple[bool, int]:
    """Returns (allowed, remaining_today). Admins are unlimited."""
    if is_admin:
        return True, -1
    if RESTRICTED_DAILY_QUOTA <= 0:
        return True, -1
    with _lock:
        now = time.time()
        bucket = [t for t in _daily_quota.get(discord_id, []) if now - t < _QUOTA_WINDOW_S]
        if len(bucket) >= RESTRICTED_DAILY_QUOTA:
            _daily_quota[discord_id] = bucket
            return False, 0
        bucket.append(now)
        _daily_quota[discord_id] = bucket
        _daily_quota.move_to_end(discord_id)
        while len(_daily_quota) > MAX_COOLDOWN_ENTRIES:
            _daily_quota.popitem(last=False)
        return True, RESTRICTED_DAILY_QUOTA - len(bucket)


def daily_quota_reset_seconds(discord_id: str, is_admin: bool) -> int:
    """Return seconds until this user's current daily quota window opens."""
    if is_admin or RESTRICTED_DAILY_QUOTA <= 0:
        return 0
    with _lock:
        now = time.time()
        bucket = [t for t in _daily_quota.get(discord_id, []) if now - t < _QUOTA_WINDOW_S]
        if not bucket:
            return 0
        return max(0, int(_QUOTA_WINDOW_S - (now - min(bucket))))


def reset_for_tests() -> None:
    """Test helper. Do not call from production code."""
    with _lock:
        _cooldowns.clear()
        _daily_quota.clear()

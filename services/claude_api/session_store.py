"""Per-channel session freshness tracking.

Ported from services/claude_sdk.py:100-180. The new tool_loop synthesizes
session IDs as UUIDs (no CLI session), but the freshness window is still
useful: bot.py uses it to skip Mem0 context refetches inside a 30-minute
conversation and to invalidate stale resumes.
"""

import time

SESSION_TTL_S = 30 * 60
_session_timestamps: dict[int, float] = {}


def _is_session_fresh(channel_id: int) -> bool:
    ts = _session_timestamps.get(channel_id)
    if ts is None:
        return False
    return (time.monotonic() - ts) < SESSION_TTL_S


def _record_session_time(channel_id: int) -> None:
    _session_timestamps[channel_id] = time.monotonic()


def _clear_session_time(channel_id: int) -> None:
    _session_timestamps.pop(channel_id, None)

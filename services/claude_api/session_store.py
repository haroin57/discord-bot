"""Per-channel session freshness tracking.

Ported from services/claude_sdk.py:100-180. The new tool_loop synthesizes
session IDs as UUIDs (no CLI session), but the freshness window is still
useful: bot.py uses it to skip Mem0 context refetches inside a 30-minute
conversation and to invalidate stale resumes.
"""

import time

SESSION_TTL_S = 30 * 60
_session_timestamps: dict[int, float] = {}
# channel_id -> model id the session was pinned to after a refusal fallback
# (ADR-0010). Cleared together with the freshness timestamp on session reset.
_session_sticky_model: dict[int, str] = {}


def _is_session_fresh(channel_id: int) -> bool:
    ts = _session_timestamps.get(channel_id)
    if ts is None:
        return False
    return (time.monotonic() - ts) < SESSION_TTL_S


def _record_session_time(channel_id: int) -> None:
    _session_timestamps[channel_id] = time.monotonic()


def _clear_session_time(channel_id: int) -> None:
    _session_timestamps.pop(channel_id, None)
    _session_sticky_model.pop(channel_id, None)


def _get_sticky_model(channel_id: int) -> str | None:
    """Return the model this session was pinned to after a refusal fallback.

    Once Fable 5 refuses at long context and we fall back to Opus, we pin the
    rest of the session to Opus. Otherwise every subsequent turn re-sends the
    full (now 200k+) context to Fable, re-pays the full cache_creation, gets
    another refusal, and falls back again — the cache never warms because the
    model alternates between Fable and Opus (observed as cache_read=0 on every
    Fable turn, ADR-0010). Pinning to Opus keeps Opus's own prompt cache warm
    across turns.

    Returns None once the 30-min freshness window lapses, so the next fresh
    session retries Fable from scratch (short context, where Fable works).
    """
    if not _is_session_fresh(channel_id):
        return None
    return _session_sticky_model.get(channel_id)


def _record_sticky_model(channel_id: int, model: str) -> None:
    """Pin this session to ``model`` for later turns (see _get_sticky_model)."""
    _session_sticky_model[channel_id] = model

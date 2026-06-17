"""Per-(channel, user) session freshness tracking.

Ported from services/claude_sdk.py:100-180. The new tool_loop synthesizes
session IDs as UUIDs (no CLI session), but the freshness window is still
useful: bot.py uses it to skip Mem0 context refetches inside a 30-minute
conversation and to invalidate stale resumes.

機能1 (2026-06-17): freshness / sticky model を (channel_id, user_id) 単位に
分離した。同一チャンネルで複数 admin (haroin / b0nchichi) が交互に発話したとき、
片方の freshness がもう片方に立つと「相手の Mem0 を引かない / 口調が混線する」
事故が起きていた。transcript (JSONL) は channel 単位で共有のまま、誰にとって
継続会話かだけを user 別に持つ。``user_id=None`` は user 不明時の legacy bucket。
"""

import time

from config import SESSION_TTL_S  # single source of truth (機能2)

# key = (channel_id, user_id | None)
_SessionKey = tuple[int, "int | None"]
# 機能2: timestamps use WALL-CLOCK (time.time()), not monotonic. monotonic is
# meaningless across a restart (the clock resets), so it cannot be persisted.
# wall-clock survives a restart, letting runtime_state.py restore freshness so
# "the conversation continues" after a bot bounce instead of forcing a full
# Mem0 refetch on the first post-restart turn.
_session_timestamps: dict[_SessionKey, float] = {}
# (channel_id, user_id) -> model id the session was pinned to after a refusal
# fallback (ADR-0010). Cleared together with the freshness timestamp on reset.
_session_sticky_model: dict[_SessionKey, str] = {}


def _is_session_fresh(channel_id: int, user_id: "int | None" = None) -> bool:
    ts = _session_timestamps.get((channel_id, user_id))
    if ts is None:
        return False
    return (time.time() - ts) < SESSION_TTL_S


def _record_session_time(channel_id: int, user_id: "int | None" = None) -> None:
    _session_timestamps[(channel_id, user_id)] = time.time()


def _clear_session_time(channel_id: int, user_id: "int | None" = None) -> None:
    _session_timestamps.pop((channel_id, user_id), None)
    _session_sticky_model.pop((channel_id, user_id), None)


def _get_sticky_model(channel_id: int, user_id: "int | None" = None) -> "str | None":
    """Return the model this (channel, user) session was pinned to after a
    refusal fallback.

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
    if not _is_session_fresh(channel_id, user_id):
        return None
    return _session_sticky_model.get((channel_id, user_id))


def _record_sticky_model(
    channel_id: int, model: str, user_id: "int | None" = None
) -> None:
    """Pin this (channel, user) session to ``model`` for later turns.

    ``model`` stays the 2nd positional arg for back-compat with existing
    callers (`_record_sticky_model(channel_id, model)`); ``user_id`` is an
    optional 3rd arg.
    """
    _session_sticky_model[(channel_id, user_id)] = model


# ---------------------------------------------------------------------------
# runtime_state persistence hooks (機能2)
# ---------------------------------------------------------------------------
def _key_to_str(key: _SessionKey) -> str:
    ch, uid = key
    return f"{ch}:{uid if uid is not None else ''}"


def _str_to_key(s: str) -> _SessionKey:
    ch, _, uid = s.partition(":")
    return (int(ch), int(uid) if uid else None)


def dump_state() -> dict:
    """Serialize freshness timestamps + sticky models for runtime_state.py."""
    return {
        "timestamps": {_key_to_str(k): v for k, v in _session_timestamps.items()},
        "sticky": {_key_to_str(k): v for k, v in _session_sticky_model.items()},
    }


def load_state(data: dict) -> None:
    """Restore freshness/sticky from disk, dropping anything past the TTL.

    Because timestamps are wall-clock, a value older than SESSION_TTL_S at
    load time is already stale and is discarded — the next turn for that
    (channel, user) starts fresh (full Mem0 refetch), exactly as if no state
    had been persisted. Sticky models are only restored for sessions whose
    freshness survived, so a stale pin can't outlive its window.
    """
    now = time.time()
    for s, ts in (data.get("timestamps") or {}).items():
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if now - ts < SESSION_TTL_S:
            _session_timestamps[_str_to_key(s)] = ts
    for s, model in (data.get("sticky") or {}).items():
        try:
            key = _str_to_key(s)
        except (ValueError, AttributeError):
            continue
        if key in _session_timestamps and isinstance(model, str):
            _session_sticky_model[key] = model

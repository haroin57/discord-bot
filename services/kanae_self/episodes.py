"""Episode ring buffer — short summaries of recent conversation turns.

Mem0 normalizes facts to ~120 chars and dedups aggressively, which is
the right shape for "what does kanae believe / prefer", but it loses the
sequence — "三時間前に X があって、その後 Y を話した". Episodes preserve
that timeline at low cost: 30-slot ring buffer of LLM-summarized turns,
queryable by recency.

Phase 2 wires `extract_kanae_signals` to emit `episode` and the bot hook
to call `append_episode` after each turn. Phase 3 will surface them in
the unified `<kanae_self>` block alongside interests + emotion.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from services.kanae_self.state import MAX_EPISODES, Episode, KanaeSelfState

_JST = timezone(timedelta(hours=9))


def _now() -> datetime:
    return datetime.now(_JST)


def _new_episode_id() -> str:
    """Short uuid4 (8 hex chars). Collisions are vanishingly unlikely
    inside a 30-slot ring buffer, and a long id wastes prompt tokens."""
    return uuid.uuid4().hex[:8]


def append_episode(
    state: KanaeSelfState,
    *,
    summary: str,
    topics: list[str] | None = None,
    valence_delta: float = 0.0,
    importance: int = 1,
    channel_id: str = "",
    now: datetime | None = None,
    episode_id: str | None = None,
) -> Episode | None:
    """Push a new episode onto the ring buffer. Returns the stored Episode.

    Returns None when `summary` is empty after stripping — a turn that
    produced no meaningful summary shouldn't take up a slot. Mutates
    `state` in place; the caller is responsible for `save_state`.

    `episode_id` can be supplied so the caller can reference the same id
    in Mem0 metadata (linking facts back to the episode they came from).
    Auto-generated when omitted.
    """
    summary_clean = (summary or "").strip()[:120]
    if not summary_clean:
        return None
    now = now or _now()
    ep = Episode(
        id=episode_id or _new_episode_id(),
        ts=now.isoformat(timespec="seconds"),
        channel_id=str(channel_id or ""),
        summary=summary_clean,
        topics=[
            str(t).strip().lower()
            for t in (topics or [])
            if str(t).strip()
        ][:3],
        valence_delta=max(-1.0, min(1.0, float(valence_delta or 0.0))),
        importance=max(1, min(5, int(importance or 1))),
    )
    state.episodes.append(ep)
    if len(state.episodes) > MAX_EPISODES:
        state.episodes = state.episodes[-MAX_EPISODES:]
    state.updated_at = ep.ts
    return ep


def recent_episodes(
    state: KanaeSelfState,
    k: int = 3,
    *,
    channel_id: str = "",
) -> list[Episode]:
    """Return up to `k` most recent episodes (newest last in ring buffer).

    When ``channel_id`` is provided, same-channel episodes are preferred:
    pull the latest k from the matching channel; if fewer than k exist,
    backfill with the most recent other-channel episodes (older first, so
    same-channel reads chronologically as the most recent context).
    Empty ``channel_id`` preserves the legacy "just take the tail" path.
    """
    if k <= 0 or not state.episodes:
        return []
    if not channel_id:
        return state.episodes[-k:]
    same = [e for e in state.episodes if e.channel_id == channel_id]
    if len(same) >= k:
        return same[-k:]
    others = [e for e in state.episodes if e.channel_id != channel_id]
    need = k - len(same)
    return others[-need:] + same

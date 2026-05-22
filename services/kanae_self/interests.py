"""Interest tracker — exponential decay + EMA upsert + top-k retrieval.

Half-life of 72h matches the design tradeoff in the plan: a topic
mentioned today is salient for ~3 days, then drops to 5% within a week.
Mem0 covers anything that needs longer persistence.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from services.kanae_self.state import (
    MAX_INTERESTS,
    SAME_CHANNEL_BONUS,
    Interest,
    KanaeSelfState,
)

# Half-life in hours. ln(2)/72 ≈ 0.00963 per hour decay constant.
DECAY_HALF_LIFE_HOURS = 72.0

# Topic touched this turn gets a salience bump after decay.
TOUCH_BUMP = 0.15

# Below this salience an Interest is GC'd. 0.05 ~= 7 days half-life
# decay from a starting salience of 1.0 — past the point where it adds
# signal to the prompt block.
GC_THRESHOLD = 0.05

# EMA weight for valence/arousal updates. 0.3 means a single new
# observation moves the attached affect by 30% toward the new value.
EMA_ALPHA = 0.3

_JST = timezone(timedelta(hours=9))


def _now() -> datetime:
    return datetime.now(_JST)


def _hours_since(iso_ts: str, now: datetime) -> float:
    """Hours between `iso_ts` and `now`. Returns 0.0 on parse failure.

    A parse failure should not amplify decay — treating it as "just now"
    keeps a malformed timestamp from accidentally erasing a topic.
    """
    if not iso_ts:
        return 0.0
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    delta = now - ts
    return max(0.0, delta.total_seconds() / 3600.0)


def _decay_factor(hours: float) -> float:
    """exp(-ln(2) * hours / half_life). 1.0 at hours=0, 0.5 at half-life."""
    if hours <= 0:
        return 1.0
    return math.exp(-math.log(2.0) * hours / DECAY_HALF_LIFE_HOURS)


def _normalize_topic(raw: str) -> str:
    """Lowercase + strip + collapse whitespace. Empty string filters out."""
    if not raw:
        return ""
    s = " ".join(str(raw).strip().lower().split())
    # Strip surrounding quotation/bracket noise that the LLM sometimes adds.
    return s.strip(" 　\t\n\"'`「」『』()[]{}")[:60]


def decay_and_upsert_interests(
    state: KanaeSelfState,
    new_signals: list[dict],
    *,
    now: datetime | None = None,
    channel_id: str = "",
) -> int:
    """Apply decay to all interests, upsert `new_signals`, GC stale topics.

    `new_signals` is a list of dicts shaped like:
        {"topic": str, "valence": float, "arousal": float}
    (the schema emitted by `extract_kanae_signals`).

    Returns the number of interests touched (upserted) in this call.
    Mutates `state` in place — caller is responsible for `save_state`.
    """
    now = now or _now()
    now_iso = now.isoformat(timespec="seconds")

    # ── 1. Decay every interest by elapsed hours since last_touched_at ─────
    for interest in state.interests.values():
        hours = _hours_since(interest.last_touched_at, now)
        interest.salience *= _decay_factor(hours)

    # ── 2. Upsert each new signal ─────────────────────────────────────────
    touched = 0
    for sig in new_signals or []:
        if not isinstance(sig, dict):
            continue
        topic = _normalize_topic(sig.get("topic", ""))
        if not topic:
            continue
        try:
            valence = float(sig.get("valence", 0.0) or 0.0)
        except (TypeError, ValueError):
            valence = 0.0
        try:
            arousal = float(sig.get("arousal", 0.0) or 0.0)
        except (TypeError, ValueError):
            arousal = 0.0
        valence = max(-1.0, min(1.0, valence))
        arousal = max(0.0, min(1.0, arousal))

        existing = state.interests.get(topic)
        if existing is None:
            state.interests[topic] = Interest(
                topic=topic,
                salience=min(1.0, 0.3 + TOUCH_BUMP),
                last_touched_at=now_iso,
                first_seen_at=now_iso,
                mention_count=1,
                valence_attached=valence,
                arousal_attached=arousal,
                channel_id=str(channel_id or ""),
            )
        else:
            existing.salience = min(1.0, existing.salience + TOUCH_BUMP)
            existing.last_touched_at = now_iso
            existing.mention_count += 1
            # EMA blend so a single outlier doesn't flip the attached
            # affect. valence_attached has more inertia than salience.
            existing.valence_attached = (
                (1 - EMA_ALPHA) * existing.valence_attached + EMA_ALPHA * valence
            )
            existing.arousal_attached = (
                (1 - EMA_ALPHA) * existing.arousal_attached + EMA_ALPHA * arousal
            )
            # Channel drifts to most-recent upsert. Empty channel_id from a
            # legacy caller preserves the prior tag instead of clobbering it.
            if channel_id:
                existing.channel_id = str(channel_id)
        touched += 1

    # ── 3. GC: drop salience-below-threshold ─────────────────────────────
    state.interests = {
        k: v for k, v in state.interests.items() if v.salience >= GC_THRESHOLD
    }

    # ── 4. Hard cap: keep top-MAX_INTERESTS by salience ──────────────────
    if len(state.interests) > MAX_INTERESTS:
        kept = sorted(
            state.interests.values(), key=lambda i: i.salience, reverse=True,
        )[:MAX_INTERESTS]
        state.interests = {i.topic: i for i in kept}

    state.updated_at = now_iso
    return touched


def top_interests(
    state: KanaeSelfState,
    k: int = 5,
    *,
    channel_id: str = "",
) -> list[Interest]:
    """Return up to `k` interests with the highest current salience.

    When `channel_id` is provided, records whose ``channel_id`` matches get
    ``SAME_CHANNEL_BONUS`` added to their salience for the sort key — local
    context is prioritized over cross-channel topics. Empty ``channel_id``
    preserves the legacy global ordering.

    Ties broken by most recently touched, then by mention_count.
    """
    if k <= 0 or not state.interests:
        return []

    def _score(it: Interest) -> tuple[float, str, int]:
        bonus = (
            SAME_CHANNEL_BONUS
            if channel_id and it.channel_id == channel_id
            else 0.0
        )
        return (it.salience + bonus, it.last_touched_at, it.mention_count)

    return sorted(state.interests.values(), key=_score, reverse=True)[:k]

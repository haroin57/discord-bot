"""Emotion transition engine — time decay + event-driven deltas.

Two pure functions on `EmotionState`:

  - `apply_time_decay(state, hours)`: loneliness/anxiety creep upward with
    silence, while every other component pulls back toward `BASELINE` via
    inertia. This is what makes "having not heard from 先輩 for 12 hours"
    feel different from "just talked to 先輩".

  - `apply_event(state, event_type, intensity)`: blend an interaction
    matrix delta into the current state with inertia and a small noise
    term. Used by the bot's per-conversation feedback hook in Phase 3.

Both return a fresh `EmotionState` — the caller is responsible for
persisting it via `services.emotion.state.save_state`.
"""

from __future__ import annotations

import random
from dataclasses import replace

from services.emotion.params import (
    ANXIETY_RATE_PER_HOUR,
    BASELINE,
    BOUNDS,
    DECAY_QUALITY_MODIFIER_MAX,
    DECAY_QUALITY_MODIFIER_MIN,
    DECAY_QUALITY_WEIGHT,
    DECAY_RESPONSIVENESS_WEIGHT,
    EVENT_DELTAS,
    INERTIA,
    LONELINESS_RATE_PER_HOUR,
    NOISE_AMPLITUDE,
)
from services.emotion.state import EmotionState

_MEAN_REVERSION_COMPONENTS = (
    "valence", "arousal", "affection", "curiosity",
)


def _clamp(value: float, key: str) -> float:
    lo, hi = BOUNDS[key]
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _quality_modifier(state: EmotionState) -> float:
    """Return the loneliness/anxiety growth multiplier driven by the last chat.

    Inputs ``quality`` and ``senpai_responsiveness`` are blended via a
    **weighted average** (not a product) so that a warm-but-unresponsive
    senpai and a terse-but-responsive one don't collapse to the same value.
    The blended 0..1 score is then linearly interpolated between MAX
    (bad chat → faster lonely growth) and MIN (good chat → slower growth).
    A fresh state with no last_interaction yet uses 1.0.
    """
    li = state.last_interaction
    if li is None or not li.timestamp:
        return 1.0
    blended = (
        DECAY_QUALITY_WEIGHT * li.quality
        + DECAY_RESPONSIVENESS_WEIGHT * li.senpai_responsiveness
    )
    raw = max(0.0, min(1.0, blended))
    return DECAY_QUALITY_MODIFIER_MAX + raw * (
        DECAY_QUALITY_MODIFIER_MIN - DECAY_QUALITY_MODIFIER_MAX
    )


def apply_time_decay(state: EmotionState, elapsed_hours: float) -> EmotionState:
    """Update `state` for `elapsed_hours` of silence.

    Loneliness grows linearly (α * h * quality_modifier, capped at 1.0).
    Anxiety creeps up at a much slower rate (also modulated). Every other
    component is pulled toward its baseline via exponential mean reversion:

        new = baseline + (cur - baseline) * gamma**elapsed_hours

    The closed-form replaces the previous discrete `for _ in range(steps)`
    loop: it gives identical math for whole hours, but is continuous in
    fractional hours (0.5h applies sqrt(gamma) of one step, not nothing),
    and is O(1) regardless of how long silence lasted.
    """
    if elapsed_hours <= 0:
        return state

    qmod = _quality_modifier(state)
    new = replace(state)
    new.loneliness = _clamp(
        state.loneliness + LONELINESS_RATE_PER_HOUR * elapsed_hours * qmod,
        "loneliness",
    )
    new.anxiety = _clamp(
        state.anxiety + ANXIETY_RATE_PER_HOUR * elapsed_hours * qmod,
        "anxiety",
    )

    # Mean reversion in closed form: identical to repeated 1-hour blending
    # for integer hours, but continuous and correct for fractional ones.
    for key in _MEAN_REVERSION_COMPONENTS:
        cur = getattr(new, key)
        baseline = BASELINE[key]
        gamma = INERTIA[key]
        cur = baseline + (cur - baseline) * (gamma ** elapsed_hours)
        setattr(new, key, _clamp(cur, key))

    return new


def apply_event(
    state: EmotionState,
    event_type: str,
    intensity: float = 1.0,
    rng: random.Random | None = None,
) -> EmotionState:
    """Blend an interaction event's delta into `state`.

    `event_type` must be a key in `EVENT_DELTAS`; unknown types are a
    silent no-op so the feedback analyzer can return e.g. "neutral" without
    needing a dedicated zero-delta entry. Each affected component is
    blended via the inertia γ and perturbed by a small uniform-random
    noise so successive identical events don't produce identical states.
    """
    if event_type not in EVENT_DELTAS:
        return state
    rng = rng or random
    deltas = EVENT_DELTAS[event_type]
    new = replace(state)
    for key, delta in deltas.items():
        cur = getattr(new, key)
        target = cur + delta * intensity
        gamma = INERTIA.get(key, 0.7)
        blended = gamma * cur + (1.0 - gamma) * target
        blended += rng.uniform(-NOISE_AMPLITUDE, NOISE_AMPLITUDE)
        setattr(new, key, _clamp(blended, key))
    return new

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

    `quality * senpai_responsiveness` is interpolated between the MAX
    (terse/bad chat → faster decay) and MIN (warm/responsive chat →
    slower decay). A fresh state with no last_interaction yet uses 1.0.
    """
    li = state.last_interaction
    if li is None or not li.timestamp:
        return 1.0
    raw = max(0.0, min(1.0, li.quality * li.senpai_responsiveness))
    return DECAY_QUALITY_MODIFIER_MAX + raw * (
        DECAY_QUALITY_MODIFIER_MIN - DECAY_QUALITY_MODIFIER_MAX
    )


def apply_time_decay(state: EmotionState, elapsed_hours: float) -> EmotionState:
    """Update `state` for `elapsed_hours` of silence.

    Loneliness grows linearly (α * h * quality_modifier, capped at 1.0).
    Anxiety creeps up at a much slower rate (also modulated). Every other
    component is pulled toward its baseline by repeated inertia blending —
    one step per whole hour. Fractional hours under 1 still apply the
    loneliness/anxiety linear growth so a tick after 10 minutes isn't a
    no-op.
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

    # Mean reversion for the remaining components. We discretize to whole
    # hours so the result is identical whether the bot ran every minute or
    # every hour for the same total elapsed time.
    steps = max(1, int(elapsed_hours))
    for key in _MEAN_REVERSION_COMPONENTS:
        cur = getattr(new, key)
        baseline = BASELINE[key]
        gamma = INERTIA[key]
        for _ in range(steps):
            cur = gamma * cur + (1.0 - gamma) * baseline
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

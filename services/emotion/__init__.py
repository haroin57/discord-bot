"""Kanae emotion architecture — teleological (purpose-driven) contact model.

See docs/kanae-emotion-architecture.md and the transition plan at
docs/2026-06-14-emotion-llm-transition-plan.md for the design rationale.

Public surface re-exports the most-used names so callers don't need to know
the internal module layout.
"""

from services.emotion.format import (
    derive_secondary_emotions,
    format_emotion_for_prompt,
)
from services.emotion.lock import LOCK_FILENAME, emotion_lock
from services.emotion.params import EVENT_DELTAS, INITIAL_STATE
from services.emotion.state import (
    EmotionState,
    LastInteraction,
    load_state,
    save_state,
)
from services.emotion.telos import Motivation, generate_telos, should_suppress
from services.emotion.timeseries import (
    append_timeseries,
    load_timeseries,
    timeseries_size_bytes,
)
from services.emotion.transition import apply_event, apply_time_decay

__all__ = [
    "EmotionState",
    "LastInteraction",
    "Motivation",
    "EVENT_DELTAS",
    "INITIAL_STATE",
    "LOCK_FILENAME",
    "emotion_lock",
    "load_state",
    "save_state",
    "append_timeseries",
    "load_timeseries",
    "timeseries_size_bytes",
    "apply_time_decay",
    "apply_event",
    "generate_telos",
    "should_suppress",
    "format_emotion_for_prompt",
    "derive_secondary_emotions",
]

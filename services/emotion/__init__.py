"""Kanae emotion architecture — teleological (purpose-driven) contact model.

See docs/kanae-emotion-architecture.md and the plan at
~/.claude/plans/mossy-noodling-bunny.md for the design rationale.

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
    "apply_time_decay",
    "apply_event",
    "generate_telos",
    "should_suppress",
    "format_emotion_for_prompt",
    "derive_secondary_emotions",
]

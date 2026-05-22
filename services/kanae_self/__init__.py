"""Kanae self-model — semi-persistent interests + episode index.

Sits between `services.emotion` (volatile 6-axis mood) and
`services.mem0` (long-term facts). Owns kanae's topic-level interests
and her episodic memory ring buffer so the bot can stay coherent with
"what she cares about lately" and "what just happened" — neither of
which the existing layers express.

Public API surface kept minimal so callers don't reach into internals:
  - load_state / save_state          (state.py)
  - decay_and_upsert_interests       (interests.py)
  - top_interests                    (interests.py)
  - append_episode                   (episodes.py)
  - format_interests_block           (format.py)
"""

from services.kanae_self.state import (
    DEFAULT_STATE_FILENAME,
    Episode,
    Interest,
    KanaeSelfState,
    load_state,
    save_state,
)

__all__ = [
    "DEFAULT_STATE_FILENAME",
    "Episode",
    "Interest",
    "KanaeSelfState",
    "load_state",
    "save_state",
]

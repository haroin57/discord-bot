"""Process-wide accessor for the persistent emotion-session daemon.

The daemon lives in bot.py's `_emotion_daemon` global, spawned on `on_ready`
and torn down on `bot.close()`. feedback.py and the silence tick run inside
the same process but in service modules that shouldn't import bot.py
(circular import, plus bot.py touches Discord globals we don't want
pulled in just to grab a subprocess handle).

This module is the thin handoff in between: bot.py calls
``set_emotion_daemon(daemon)`` after a successful spawn, callers fetch via
``get_emotion_daemon()`` and null-check before use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.emotion.persistent_session import EmotionSessionDaemon

_daemon: "EmotionSessionDaemon | None" = None


def set_emotion_daemon(daemon: "EmotionSessionDaemon | None") -> None:
    """Register (or clear, on shutdown) the active emotion daemon."""
    global _daemon
    _daemon = daemon


def get_emotion_daemon() -> "EmotionSessionDaemon | None":
    """Return the active daemon, or None if not yet started / already stopped.

    Callers must null-check and fall back gracefully — the daemon may have
    failed to spawn, may be mid-restart, or this may be a test process
    where the bot's on_ready never ran.
    """
    return _daemon

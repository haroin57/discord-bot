"""Process-wide context for the persistent emotion-session daemon.

The daemon is a long-lived Claude CLI subprocess spawned by bot.py's
`on_ready` and shut down in `bot.close()`. feedback.py and the silence
tick run in the same process but in service modules that mustn't import
bot.py (circular import, plus bot.py drags in Discord globals).

`EMOTION_CTX` is the thin handoff between them. bot.py sets the daemon
after a successful spawn; callers fetch via `EMOTION_CTX.get()` and
null-check before use. `is_available()` packages the typical
"daemon exists and is alive" check.

Phase 5 M1 replaced the previous module-level `_daemon` global +
`set_emotion_daemon` / `get_emotion_daemon` free functions with this
context object so tests can inject a mock daemon cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.emotion.persistent_session import EmotionSessionDaemon


class EmotionDaemonContext:
    """Holds a single optional daemon reference for the process."""

    __slots__ = ("_daemon",)

    def __init__(self) -> None:
        self._daemon: "EmotionSessionDaemon | None" = None

    def set(self, daemon: "EmotionSessionDaemon | None") -> None:
        """Register (or clear, on shutdown) the active emotion daemon."""
        self._daemon = daemon

    def get(self) -> "EmotionSessionDaemon | None":
        """Return the daemon, or None if not running / not yet spawned."""
        return self._daemon

    def is_available(self) -> bool:
        """True iff a daemon is registered and reports itself alive."""
        d = self._daemon
        return d is not None and getattr(d, "is_alive", False)


# Module-level singleton — bot.py and feedback.py both reach for the same one.
EMOTION_CTX = EmotionDaemonContext()

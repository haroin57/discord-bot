"""Emotion feedback loop — analyze interaction + update state.

Called from bot.py / web_api.py right after a successful Claude response.
Runs as a fire-and-forget background task so it never adds latency to the
reply send. The whole call is wrapped in a try/except so an analyzer
failure or lock timeout cannot break the calling path.

Threading model:
- analyze_interaction is async (anthropic Haiku call) — awaited directly.
- load_state / apply_event / save_state are sync, and emotion_lock is a
  sync context manager. We run the lock-protected RMW inside
  asyncio.to_thread so the event loop is never blocked even if the lock
  is contended by the cron tick (kanae_emotion_tick.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from pathlib import Path

from services.emotion.analyzer import analyze_interaction
from services.emotion.lock import LOCK_FILENAME, emotion_lock
from services.emotion.state import LastInteraction, load_state, save_state
from services.emotion.transition import apply_event

log = logging.getLogger(__name__)

STATE_FILENAME = "kanae_emotion.json"

# Cron tick poll interval is fine-grained (kanae_emotion_tick.py acquires
# the same flock for its decay+telos RMW). A 5 s budget is more than enough
# for that tick to release; longer would just delay the bot's housekeeping
# without benefit.
_LOCK_TIMEOUT_S = 5.0


async def schedule_feedback(
    *,
    user_text: str,
    assistant_text: str,
    state_dir: str | Path,
    history_tail: str = "",
) -> None:
    """Apply one feedback event derived from a finished turn.

    Designed to be called as ``asyncio.create_task(schedule_feedback(...))``
    from the bot. ``state_dir`` is the directory holding kanae_emotion.json
    and kanae_emotion.lock — passed in (not imported from config) so the
    same function works for both the Discord bot and the web API path and
    so tests can point it at a tmp dir.

    All exceptions are caught and logged; never raises.
    """
    state_dir = Path(state_dir)
    state_path = state_dir / STATE_FILENAME
    lock_path = state_dir / LOCK_FILENAME

    try:
        event = await analyze_interaction(
            user_text=user_text,
            assistant_text=assistant_text,
            history_tail=history_tail,
        )
    except Exception as exc:  # analyzer should already self-handle, defense in depth
        log.warning("emotion analyze_interaction failed: %s", exc)
        return

    try:
        await asyncio.to_thread(_apply_and_save, event, state_path, lock_path)
    except TimeoutError:
        log.warning("emotion feedback: lock contended for >%ss, skipping", _LOCK_TIMEOUT_S)
    except Exception as exc:
        log.warning("emotion feedback RMW failed: %s", exc)


def _apply_and_save(event: dict, state_path: Path, lock_path: Path) -> None:
    """Synchronous RMW under flock. Runs inside ``asyncio.to_thread``."""
    with emotion_lock(lock_path, blocking=True, timeout=_LOCK_TIMEOUT_S):
        state = load_state(state_path)
        state = apply_event(
            state,
            event.get("event_type", "short_reply"),
            float(event.get("intensity", 0.5)),
        )
        state = dataclasses.replace(
            state,
            last_interaction=LastInteraction(
                timestamp=datetime.now(timezone.utc).isoformat(),
                quality=float(event.get("intimacy_level", 0.5)),
                initiated_by="senpai",
                topics=list(event.get("topics") or [])[:5],
                duration_minutes=0.0,
                senpai_responsiveness=float(event.get("senpai_engagement", 0.5)),
            ),
        )
        save_state(state, state_path)
        log.info(
            "emotion feedback applied: event=%s intensity=%.2f topics=%s",
            event.get("event_type"), event.get("intensity"), event.get("topics"),
        )

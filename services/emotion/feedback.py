"""Emotion feedback loop — LLM-based transition on each conversation turn.

Called from bot.py right after a successful Claude response. Runs as a
fire-and-forget background task so it never adds latency to the reply send.

2026-06-14 v3: Switched to the persistent emotion-session daemon (held in
`services.emotion.runtime`). The daemon is shared with the silence tick;
both paths await `daemon.query()` serialized by an `asyncio.Lock` inside
the daemon. We flag `is_conversation_active=True` for the duration so the
silence tick can skip its turn rather than queue behind in-flight feedback.

Fall-through: if the daemon isn't ready (failed to spawn / hasn't started
yet / mid-restart) we skip this turn rather than block on the legacy
subprocess.run path. The next turn will retry.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from services.emotion.lock import LOCK_FILENAME, emotion_lock
from services.emotion.state import load_state, save_state

log = logging.getLogger(__name__)

STATE_FILENAME = "kanae_emotion.json"
_LOCK_TIMEOUT_S = 5.0


async def schedule_feedback(
    *,
    user_text: str,
    assistant_text: str,
    state_dir: str | Path,
    history_tail: str = "",
) -> None:
    """Apply one LLM-based emotion update derived from a finished turn.

    Designed to be called as ``asyncio.create_task(schedule_feedback(...))``
    from the bot. All exceptions are caught and logged; never raises.
    """
    state_dir = Path(state_dir)

    from services.emotion.runtime import get_emotion_daemon
    daemon = get_emotion_daemon()
    if daemon is None or not daemon.is_alive:
        log.info("emotion feedback: daemon unavailable, skipping turn")
        return

    daemon.is_conversation_active = True
    try:
        await _daemon_transition_and_save(
            daemon=daemon,
            user_text=user_text,
            assistant_text=assistant_text,
            history_tail=history_tail,
            state_dir=state_dir,
        )
    except Exception as exc:
        log.warning("emotion feedback failed: %s", exc)
    finally:
        # Always release the flag even on failure so the silence tick
        # doesn't get permanently locked out.
        daemon.is_conversation_active = False


async def _daemon_transition_and_save(
    *,
    daemon,
    user_text: str,
    assistant_text: str,
    history_tail: str,
    state_dir: Path,
) -> None:
    """Daemon-backed LLM transition + atomic state save."""
    from services.emotion.llm_transition import llm_transition_via_daemon

    state_path = state_dir / STATE_FILENAME
    lock_path = state_dir / LOCK_FILENAME

    state = await asyncio.to_thread(load_state, state_path)
    # validator stamps last_interaction from conversation_meta — no need
    # for a separate dataclasses.replace pass here.
    new_state = await llm_transition_via_daemon(
        state,
        daemon,
        mode="conversation",
        state_dir=state_dir,
        user_text=user_text,
        assistant_text=assistant_text,
        history_tail=history_tail,
        conversation_meta={
            "topics": [],
            "quality": 0.7,
            "senpai_engagement": 0.7,
        },
    )
    if new_state is None:
        log.warning("emotion feedback: daemon returned None, keeping previous state")
        return

    try:
        await asyncio.to_thread(
            _save_under_lock, new_state, state_path, lock_path
        )
    except TimeoutError:
        log.warning("emotion feedback: state-file lock contended >%ss, skip save",
                    _LOCK_TIMEOUT_S)
        return

    log.info(
        "emotion feedback (daemon): v=%.2f lo=%.2f af=%.2f",
        new_state.valence, new_state.loneliness, new_state.affection,
    )


def _save_under_lock(state, state_path: Path, lock_path: Path) -> None:
    """Synchronous save with the shared flock; runs inside to_thread."""
    with emotion_lock(lock_path, blocking=True, timeout=_LOCK_TIMEOUT_S):
        save_state(state, state_path)

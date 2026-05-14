#!/usr/bin/env python3
"""CLI for emotion feedback — update kanae_emotion.json from a conversation turn.

Usage:
    # Full Haiku analysis (requires ANTHROPIC_API_KEY):
    python3 emotion_feedback_cli.py --user "user text" --assistant "assistant text"

    # Direct event application (no Haiku, fast):
    python3 emotion_feedback_cli.py --event sweet_words --intensity 0.8

Runs the same analyzer + RMW pipeline as the Discord bot's
services.emotion.feedback module, but callable from a shell (e.g. Claude
Code's Bash tool).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add discord-bot to path
sys.path.insert(0, "/home/ubuntu/discord-bot")

from services.emotion.analyzer import analyze_interaction
from services.emotion.lock import emotion_lock
from services.emotion.state import LastInteraction, load_state, save_state
from services.emotion.transition import apply_event

STATE_DIR = Path("/home/ubuntu/discord-bot/.state")
STATE_PATH = STATE_DIR / "kanae_emotion.json"
LOCK_PATH = STATE_DIR / "kanae_emotion.lock"
LOCK_TIMEOUT = 5.0

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _apply_and_save(event: dict) -> None:
    """Synchronous RMW under flock — same as feedback._apply_and_save."""
    with emotion_lock(LOCK_PATH, blocking=True, timeout=LOCK_TIMEOUT):
        state = load_state(STATE_PATH)
        state = apply_event(
            state,
            event.get("event_type", "short_reply"),
            float(event.get("intensity", 0.5)),
        )
        state = dataclasses.replace(
            state,
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_interaction=LastInteraction(
                timestamp=datetime.now(timezone.utc).isoformat(),
                quality=float(event.get("intimacy_level", 0.5)),
                initiated_by="senpai",
                topics=list(event.get("topics") or [])[:5],
                duration_minutes=0.0,
                senpai_responsiveness=float(event.get("senpai_engagement", 0.5)),
            ),
        )
        save_state(state, STATE_PATH)


async def run(user_text: str, assistant_text: str) -> None:
    log.info("Analyzing interaction...")
    event = await analyze_interaction(
        user_text=user_text,
        assistant_text=assistant_text,
    )
    log.info(
        "Event: %s (intensity=%.2f, topics=%s)",
        event.get("event_type"),
        event.get("intensity", 0),
        event.get("topics"),
    )
    _apply_and_save(event)
    log.info("State updated.")


def _apply_direct(event_type: str, intensity: float) -> None:
    """Apply an event directly without Haiku analysis."""
    with emotion_lock(LOCK_PATH, blocking=True, timeout=LOCK_TIMEOUT):
        state = load_state(STATE_PATH)
        state = apply_event(state, event_type, intensity)
        state = dataclasses.replace(
            state,
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_interaction=LastInteraction(
                timestamp=datetime.now(timezone.utc).isoformat(),
                quality=intensity,
                initiated_by="senpai",
                topics=[],
                duration_minutes=0.0,
                senpai_responsiveness=0.5,
            ),
        )
        save_state(state, STATE_PATH)
    log.info("Direct event applied: %s (intensity=%.2f)", event_type, intensity)


def main() -> None:
    parser = argparse.ArgumentParser(description="Emotion feedback CLI")
    parser.add_argument("--user", help="User (senpai) message text")
    parser.add_argument("--assistant", help="Assistant (kanae) response text")
    parser.add_argument("--event", help="Direct event type (skip Haiku)")
    parser.add_argument("--intensity", type=float, default=0.7,
                        help="Event intensity 0.0-1.0 (default 0.7)")
    args = parser.parse_args()

    if args.event:
        _apply_direct(args.event, max(0.0, min(1.0, args.intensity)))
    elif args.user and args.assistant:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.error("ANTHROPIC_API_KEY not set")
            sys.exit(1)
        asyncio.run(run(args.user, args.assistant))
    else:
        parser.error("Either --event or both --user and --assistant required")


if __name__ == "__main__":
    main()

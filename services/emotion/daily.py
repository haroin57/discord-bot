"""Daily journal — ring buffer + incremental summary for kanae_daily.

Storage:
  - `.state/kanae_daily.json`    — ring buffer of 96 entries (~24h)
  - `.state/kanae_daily_summary.txt` — running ~500-token summary
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from utils.atomic_write import atomic_write_json, atomic_write_text

log = logging.getLogger(__name__)

DAILY_BUFFER_SIZE = 96
DAILY_FILENAME = "kanae_daily.json"
SUMMARY_FILENAME = "kanae_daily_summary.txt"


@dataclass
class DailyEntry:
    ts: str
    event_type: str
    importance: int
    activity: str
    detail: str
    mood: str
    thought: str

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "event_type": self.event_type,
            "importance": self.importance,
            "activity": self.activity,
            "detail": self.detail,
            "mood": self.mood,
            "thought": self.thought,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DailyEntry:
        return cls(
            ts=str(d.get("ts", "")),
            event_type=str(d.get("event_type", "routine")),
            importance=int(d.get("importance", 3)),
            activity=str(d.get("activity", "")),
            detail=str(d.get("detail", "")),
            mood=str(d.get("mood", "normal")),
            thought=str(d.get("thought", "")),
        )


def load_daily(state_dir: str | os.PathLike) -> list[DailyEntry]:
    """Load daily journal entries from disk."""
    p = Path(state_dir) / DAILY_FILENAME
    if not p.is_file():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("daily journal load failed: %s", exc)
        return []
    entries_raw = data.get("entries", []) if isinstance(data, dict) else []
    return [DailyEntry.from_dict(e) for e in entries_raw if isinstance(e, dict)]


def save_daily(entries: list[DailyEntry], state_dir: str | os.PathLike) -> None:
    """Persist daily journal (ring buffer, max DAILY_BUFFER_SIZE)."""
    entries = entries[-DAILY_BUFFER_SIZE:]
    p = Path(state_dir) / DAILY_FILENAME
    atomic_write_json(p, {"entries": [e.to_dict() for e in entries]})


def load_summary(state_dir: str | os.PathLike) -> str:
    """Load the incremental daily summary."""
    p = Path(state_dir) / SUMMARY_FILENAME
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_summary(text: str, state_dir: str | os.PathLike) -> None:
    """Persist the daily summary."""
    p = Path(state_dir) / SUMMARY_FILENAME
    atomic_write_text(p, text)


def format_recent_entries(entries: list[DailyEntry], count: int = 8) -> str:
    """Format the most recent N entries for prompt injection.

    detail and thought are capped to keep the block as a concise hint,
    not a narrative the LLM roleplays from.
    """
    recent = entries[-count:]
    if not recent:
        return "(まだ日常ログがありません)"
    lines = []
    for e in reversed(recent):
        lines.append(
            f"[{e.ts}] {e.activity} — {e.detail[:120]} "
            f"(mood:{e.mood}, thought:{e.thought[:50]})"
        )
    return "\n".join(lines)

"""Daily journal — ring buffer + incremental summary for kanae_daily.

Storage:
  - `.state/kanae_daily.json`    — ring buffer of 96 entries (~24h)
  - `.state/kanae_daily_summary.txt` — running ~500-token summary
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

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
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(
        {"entries": [e.to_dict() for e in entries]},
        ensure_ascii=False, indent=2,
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        fd = None
        os.replace(tmp, p)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        fd = None
        os.replace(tmp, p)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def format_recent_entries(entries: list[DailyEntry], count: int = 8) -> str:
    """Format the most recent N entries for prompt injection."""
    recent = entries[-count:]
    if not recent:
        return "(まだ日常ログがありません)"
    lines = []
    for e in reversed(recent):
        lines.append(
            f"[{e.ts}] {e.activity} — {e.detail[:200]} "
            f"(mood:{e.mood}, thought:{e.thought[:100]})"
        )
    return "\n".join(lines)

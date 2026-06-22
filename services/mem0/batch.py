"""Per-channel turn accumulator for batch Mem0 ingestion.

Instead of writing to Mem0 every turn, turns are accumulated on disk and
flushed as a single batch when the count reaches BATCH_SIZE (default 10).
This reduces Haiku extraction calls from N to 1 per batch window.

State files live under ``.state/mem0_batch/{channel_id}.json`` and survive
bot restarts. The next session picks up where the previous one left off.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

BATCH_SIZE = int(os.environ.get("MEM0_BATCH_SIZE", "10"))


def _batch_dir(state_dir: str | os.PathLike) -> Path:
    d = Path(state_dir) / "mem0_batch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _batch_file(state_dir: str | os.PathLike, channel_id: int) -> Path:
    return _batch_dir(state_dir) / f"{channel_id}.json"


def _load_raw(state_dir: str | os.PathLike, channel_id: int) -> dict:
    p = _batch_file(state_dir, channel_id)
    if not p.is_file():
        return {"turns": [], "kanae_facts": []}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"turns": [], "kanae_facts": []}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("batch load failed for ch=%d: %s", channel_id, exc)
        return {"turns": [], "kanae_facts": []}


def _save_raw(state_dir: str | os.PathLike, channel_id: int, data: dict) -> None:
    p = _batch_file(state_dir, channel_id)
    atomic_write_json(p, data)


def append_turn(
    state_dir: str | os.PathLike,
    channel_id: int,
    user_text: str,
    assistant_text: str,
) -> int:
    """Append a conversation turn and return the updated count."""
    data = _load_raw(state_dir, channel_id)
    turns = data.get("turns", [])
    turns.append({
        "user": user_text[:2000],
        "assistant": assistant_text[:3000],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    data["turns"] = turns
    _save_raw(state_dir, channel_id, data)
    return len(turns)


def build_batch_summary(turns: list[dict], turn_chars: int | None = None) -> str:
    """Render accumulated turns into the ``user: …\\nassistant: …`` summary fed
    to the extractor. Each turn's user/assistant text is capped at
    ``turn_chars`` (default ``MEM0_BATCH_TURN_CHARS``). Kept here, pure and
    testable, instead of inline in bot.py so the truncation budget is one
    documented knob rather than a magic ``[:300]``.
    """
    if turn_chars is None:
        try:
            from config import MEM0_BATCH_TURN_CHARS
            turn_chars = MEM0_BATCH_TURN_CHARS
        except Exception:
            turn_chars = 600
    return "\n".join(
        f"user: {(t.get('user') or '')[:turn_chars]}\n"
        f"assistant: {(t.get('assistant') or '')[:turn_chars]}"
        for t in (turns or [])
    )


def append_kanae_facts(
    state_dir: str | os.PathLike,
    channel_id: int,
    facts: list[str],
) -> None:
    """Accumulate kanae-extracted facts for batch flush."""
    if not facts:
        return
    data = _load_raw(state_dir, channel_id)
    existing = data.get("kanae_facts", [])
    existing.extend(facts)
    data["kanae_facts"] = existing
    _save_raw(state_dir, channel_id, data)


def pop_batch(
    state_dir: str | os.PathLike,
    channel_id: int,
) -> tuple[list[dict], list[str]]:
    """Return all accumulated turns + kanae facts and reset the file.

    Returns ``(turns, kanae_facts)``.
    """
    data = _load_raw(state_dir, channel_id)
    turns = data.get("turns", [])
    kanae_facts = data.get("kanae_facts", [])
    _save_raw(state_dir, channel_id, {"turns": [], "kanae_facts": []})
    return turns, kanae_facts

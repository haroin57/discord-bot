"""Session and conversation persistence — matches free-code utils/sessionStorage.ts pattern.

JSONL append-only transcripts per channel, JSON for SDK session IDs.
"""

import json
import logging
import os
import time
from collections import OrderedDict

from config import (
    HISTORY_DIR,
    MAX_HISTORY,
    MAX_MESSAGES_PER_CHANNEL,
    MAX_SESSIONS,
    SESSIONS_PATH,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SDK session IDs (channel_id -> session_id for --resume)
# ---------------------------------------------------------------------------
def load_sessions() -> OrderedDict[int, str]:
    try:
        with open(SESSIONS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return OrderedDict((int(k), v) for k, v in data.items())
    except (FileNotFoundError, json.JSONDecodeError):
        return OrderedDict()


def save_sessions(s: OrderedDict[int, str]) -> None:
    with open(SESSIONS_PATH, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in s.items()}, f)


def update_session(sessions: OrderedDict[int, str], channel_id: int, session_id: str) -> None:
    sessions[channel_id] = session_id
    sessions.move_to_end(channel_id)
    while len(sessions) > MAX_SESSIONS:
        sessions.popitem(last=False)
    save_sessions(sessions)


# ---------------------------------------------------------------------------
# JSONL conversation history (free-code: {sessionId}.jsonl)
# ---------------------------------------------------------------------------
def _history_file(channel_id: int) -> str:
    return os.path.join(HISTORY_DIR, f"{channel_id}.jsonl")


def append_jsonl(channel_id: int, entry: dict) -> None:
    """Append a single entry to the channel's JSONL transcript (fire-and-forget safe)."""
    entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    entry["channel_id"] = channel_id
    with open(_history_file(channel_id), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history_channel(channel_id: int) -> list[dict]:
    """Load conversation messages for a channel from JSONL transcript."""
    path = _history_file(channel_id)
    messages: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("role") in ("user", "assistant"):
                        messages.append({"role": entry["role"], "content": entry["content"]})
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return messages


def load_all_history() -> OrderedDict[int, list[dict]]:
    """Load all conversation histories from JSONL files."""
    result: OrderedDict[int, list[dict]] = OrderedDict()
    try:
        for fname in sorted(os.listdir(HISTORY_DIR)):
            if not fname.endswith(".jsonl"):
                continue
            channel_id = int(fname.replace(".jsonl", ""))
            msgs = load_history_channel(channel_id)
            if msgs:
                result[channel_id] = msgs
    except (FileNotFoundError, ValueError):
        pass
    while len(result) > MAX_HISTORY:
        result.popitem(last=False)
    return result


# ---------------------------------------------------------------------------
# History integrity (free-code: deserializeMessagesWithInterruptDetection)
# ---------------------------------------------------------------------------
def trim_history(history: list[dict], max_pairs: int = MAX_MESSAGES_PER_CHANNEL) -> None:
    """Trim history to max_pairs user/assistant exchanges, keeping pairs intact."""
    max_entries = max_pairs * 2
    if len(history) <= max_entries:
        return
    start = len(history) - max_entries
    # Ensure we start on a user message (pair boundary)
    while start < len(history) and history[start]["role"] != "user":
        start += 1
    del history[:start]


def fix_history_integrity(history: list[dict]) -> None:
    """Fix interrupted sessions — ensure valid user/assistant alternation for API."""
    # Remove dangling user message at end (interrupted mid-turn)
    if history and history[-1]["role"] == "user":
        history.pop()
    # Ensure first message is user (API requirement)
    while history and history[0]["role"] != "user":
        history.pop(0)

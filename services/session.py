"""Session and conversation persistence — matches free-code utils/sessionStorage.ts pattern.

JSONL append-only transcripts per channel, JSON for SDK session IDs.
"""

import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from config import (
    HISTORY_DIR,
    MAX_HISTORY,
    MAX_MESSAGES_PER_CHANNEL,
    MAX_SESSIONS,
    SESSIONS_PATH,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SessionInfo — rich metadata per channel session
# ---------------------------------------------------------------------------
@dataclass
class SessionInfo:
    session_id: str
    mcp_server_keys: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SDK session IDs (channel_id -> SessionInfo for --resume)
# ---------------------------------------------------------------------------
def load_sessions() -> OrderedDict[int, SessionInfo]:
    try:
        with open(SESSIONS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        result: OrderedDict[int, SessionInfo] = OrderedDict()
        for k, v in data.items():
            if isinstance(v, str):
                # Backward compat: old format was plain session_id string
                result[int(k)] = SessionInfo(session_id=v)
            elif isinstance(v, dict):
                result[int(k)] = SessionInfo(
                    session_id=v["session_id"],
                    mcp_server_keys=v.get("mcp_server_keys", []),
                )
        return result
    except (FileNotFoundError, json.JSONDecodeError):
        return OrderedDict()


def save_sessions(s: OrderedDict[int, SessionInfo]) -> None:
    data = {
        str(k): {
            "session_id": v.session_id,
            "mcp_server_keys": v.mcp_server_keys,
        }
        for k, v in s.items()
    }
    with open(SESSIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def update_session(
    sessions: OrderedDict[int, SessionInfo],
    channel_id: int,
    info: SessionInfo,
) -> None:
    sessions[channel_id] = info
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
    # Explicit buffering avoids tiny unbuffered writes on spinning disks.
    with open(_history_file(channel_id), "a", encoding="utf-8", buffering=8192) as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _tail_jsonl(path: str, max_entries: int) -> list[dict]:
    """Read the last `max_entries` valid role='user'/'assistant' rows from a JSONL file.

    Reads from the end of the file in chunks to avoid loading huge transcripts
    into memory on hot-path access.
    """
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = 8192
            data = b""
            lines_found = 0
            pos = size
            # Read chunks from the tail until we have enough newlines or hit BOF.
            while pos > 0 and lines_found <= max_entries * 2:
                read_size = min(chunk, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size) + data
                lines_found = data.count(b"\n")
            text = data.decode("utf-8", errors="replace")
    except OSError:
        return []

    messages: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("role") in ("user", "assistant"):
            messages.append({"role": entry["role"], "content": entry["content"]})
    if len(messages) > max_entries:
        messages = messages[-max_entries:]
    return messages


def load_history_channel(channel_id: int) -> list[dict]:
    """Load recent conversation messages for a channel (tail only)."""
    return _tail_jsonl(_history_file(channel_id), MAX_MESSAGES_PER_CHANNEL * 2)


class LazyHistory(OrderedDict):
    """LRU-bounded OrderedDict that lazy-loads channel tails from JSONL on first access."""

    def __missing__(self, channel_id: int):
        path = os.path.join(HISTORY_DIR, f"{channel_id}.jsonl")
        msgs = _tail_jsonl(path, MAX_MESSAGES_PER_CHANNEL * 2)
        self[channel_id] = msgs
        # LRU bound: drop oldest channels beyond MAX_HISTORY.
        while len(self) > MAX_HISTORY:
            self.popitem(last=False)
        return msgs


def load_all_history() -> LazyHistory:
    """Eagerly scan HISTORY_DIR on startup (tail-loading each channel).

    Returns a LazyHistory so subsequent lookups for unseen channels still
    work via LazyHistory.__missing__.
    """
    result = LazyHistory()
    try:
        for fname in sorted(os.listdir(HISTORY_DIR)):
            if not fname.endswith(".jsonl"):
                continue
            try:
                channel_id = int(fname.replace(".jsonl", ""))
            except ValueError:
                continue
            msgs = _tail_jsonl(os.path.join(HISTORY_DIR, fname), MAX_MESSAGES_PER_CHANNEL * 2)
            if msgs:
                result[channel_id] = msgs
    except FileNotFoundError:
        pass
    # Drop oldest channels over the LRU limit.
    while len(result) > MAX_HISTORY:
        result.popitem(last=False)
    return result


# ---------------------------------------------------------------------------
# History integrity (free-code: deserializeMessagesWithInterruptDetection)
# ---------------------------------------------------------------------------
def trim_history(history: list[dict], max_pairs: int = MAX_MESSAGES_PER_CHANNEL) -> None:
    """Trim history to max_pairs user/assistant exchanges, keeping pairs intact.

    The previous implementation walked `start` forward to find a user-role
    boundary with no upper bound. When the tail happened to be all-assistant
    (e.g. retry storms that re-appended bot replies), `start` would walk to
    `len(history)` and the subsequent `del history[:start]` would wipe the
    entire conversation context for that channel. We now bound the forward
    scan and fall back to the most recent user message so history is never
    fully deleted.
    """
    max_entries = max_pairs * 2
    if len(history) <= max_entries:
        return
    start = len(history) - max_entries
    # Forward scan: at most 2 positions, never past the keep window itself.
    scan_limit = min(start + 2, len(history))
    while start < scan_limit and history[start]["role"] != "user":
        start += 1
    if start >= len(history) or history[start]["role"] != "user":
        # Forward scan exhausted without finding a user boundary. Fall back
        # to the latest user message anywhere in the history so the tail
        # always contains at least one complete pair instead of getting
        # wiped entirely.
        fallback = len(history) - 1
        while fallback >= 0 and history[fallback]["role"] != "user":
            fallback -= 1
        start = max(0, fallback)
    del history[:start]


def fix_history_integrity(history: list[dict]) -> None:
    """Ensure history starts with user and ends with assistant."""
    # Remove trailing user message (incomplete exchange)
    if history and history[-1]["role"] == "user":
        history.pop()
    # Find first user message and remove everything before it
    first_user = 0
    while first_user < len(history) and history[first_user].get("role") != "user":
        first_user += 1
    if first_user > 0:
        del history[:first_user]

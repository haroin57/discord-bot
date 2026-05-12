"""Session and conversation persistence — matches free-code utils/sessionStorage.ts pattern.

JSONL append-only transcripts per channel, JSON for SDK session IDs.
"""

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from config import (
    HISTORY_DIR,
    MAX_MESSAGES_PER_CHANNEL,
    MAX_SESSIONS,
    SESSIONS_PATH,
)

log = logging.getLogger(__name__)

# Background save tasks for `update_session`. We keep a strong reference so
# the task is not GC'd before completion; the done callback removes itself.
_save_tasks: set[asyncio.Task] = set()


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
    # Open with 0o600 so SDK session_ids (potentially session-resumable secrets)
    # are not world-readable on a shared host. O_NOFOLLOW refuses to follow a
    # symlink at SESSIONS_PATH so a local attacker cannot redirect the write.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(SESSIONS_PATH, flags, 0o600)
    # fchmod enforces 0o600 even when the file pre-existed with looser
    # permissions (os.open's mode argument is only applied on create).
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)


def update_session(
    sessions: OrderedDict[int, SessionInfo],
    channel_id: int,
    info: SessionInfo,
) -> None:
    """Mutate the session dict in-place and schedule a non-blocking save.

    The dict mutation is synchronous (cheap, single hash lookup + LRU pop)
    but the disk write is offloaded to a worker thread via `asyncio.to_thread`
    so the event loop does not stall on every Discord message that produces
    a new SDK session ID. Falls back to a synchronous save when called
    outside an event loop (tests / CLI scripts).
    """
    sessions[channel_id] = info
    sessions.move_to_end(channel_id)
    while len(sessions) > MAX_SESSIONS:
        sessions.popitem(last=False)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — direct sync save (tests / one-shot CLI usage).
        save_sessions(sessions)
        return
    task = loop.create_task(asyncio.to_thread(save_sessions, sessions))
    _save_tasks.add(task)
    task.add_done_callback(_save_tasks.discard)


# ---------------------------------------------------------------------------
# JSONL conversation history (free-code: {sessionId}.jsonl)
# ---------------------------------------------------------------------------
def _history_file(channel_id: int) -> str:
    return os.path.join(HISTORY_DIR, f"{channel_id}.jsonl")


def append_jsonl(channel_id: int, entry: dict) -> None:
    """Append a single entry to the channel's JSONL transcript (fire-and-forget safe)."""
    entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    entry["channel_id"] = channel_id
    # Open with 0o600 so conversation transcripts are not world-readable on a
    # shared host. O_APPEND keeps the append-only semantics; O_NOFOLLOW refuses
    # to follow a symlink at the history-file path.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(_history_file(channel_id), flags, 0o600)
    # fchmod enforces 0o600 even when the file pre-existed with looser
    # permissions (the mode argument is only applied on create).
    os.fchmod(fd, 0o600)
    # Explicit buffering avoids tiny unbuffered writes on spinning disks.
    with os.fdopen(fd, "a", encoding="utf-8", buffering=8192) as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _tail_jsonl(path: str, max_entries: int) -> list[dict]:
    """Read the last `max_entries` valid role='user'/'assistant' rows from a JSONL file.

    Reads from the end of the file in chunks to avoid loading huge transcripts
    into memory on hot-path access. Accumulates chunks in a list and joins
    once at the end so the buffer concatenation is O(n) instead of the
    previous `data = read() + data` pattern which was O(n²) in memory churn
    (1 MB file → ~64 MB intermediate allocation under the old pattern).
    """
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = 8192
            chunks: list[bytes] = []
            lines_found = 0
            pos = size
            # Read chunks from the tail until we have enough newlines or hit BOF.
            while pos > 0 and lines_found <= max_entries * 2:
                read_size = min(chunk, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size)
                chunks.insert(0, buf)
                # Count newlines in the new chunk only — total grows monotonically.
                lines_found += buf.count(b"\n")
            text = b"".join(chunks).decode("utf-8", errors="replace")
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


# `LazyHistory` and `load_all_history` were removed in Phase D — they were
# referenced only by tests, never by production code (bot.py calls
# load_history_channel directly via asyncio.to_thread). Eager startup-time
# scanning of every JSONL file blocked the event loop and the LazyHistory
# dict was never consulted on the hot path.


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

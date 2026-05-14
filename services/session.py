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

# Internal composite-key store: (channel_id, user_id | None) -> SessionInfo
# user_id=None is the legacy fallback bucket for callers that don't supply it.
_sessions_store: OrderedDict[tuple[int, int | None], SessionInfo] = OrderedDict()


def get_session(channel_id: int, user_id: int | None) -> "SessionInfo | None":
    """Return the SessionInfo for (channel_id, user_id), or None if absent."""
    return _sessions_store.get((channel_id, user_id))


def set_session(channel_id: int, user_id: int | None, info: "SessionInfo") -> None:
    """Store info under composite key and schedule a non-blocking disk save."""
    key = (channel_id, user_id)
    _sessions_store[key] = info
    _sessions_store.move_to_end(key)
    while len(_sessions_store) > MAX_SESSIONS:
        _sessions_store.popitem(last=False)
    _schedule_save()


def clear_session(channel_id: int, user_id: int | None) -> None:
    """Remove the session for (channel_id, user_id) if present."""
    _sessions_store.pop((channel_id, user_id), None)
    _schedule_save()


def _schedule_save() -> None:
    """Flush _sessions_store to disk, async if a loop is running."""
    flat = _flatten_sessions(_sessions_store)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        save_sessions(flat)
        return
    snapshot = OrderedDict(flat)
    task = loop.create_task(asyncio.to_thread(save_sessions, snapshot))
    _save_tasks.add(task)
    task.add_done_callback(_on_save_done)


def _flatten_sessions(
    store: "OrderedDict[tuple[int, int | None], SessionInfo]",
) -> "OrderedDict[int, SessionInfo]":
    """Convert composite-key store to the channel_id-keyed format for disk.

    For each channel_id the last seen entry wins (user_id=None legacy bucket
    is included unless a user-specific entry for the same channel exists later
    in insertion order).
    """
    flat: OrderedDict[int, SessionInfo] = OrderedDict()
    for (ch, _uid), info in store.items():
        flat[ch] = info
    return flat


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
        # Populate internal store from disk (legacy bucket, user_id=None)
        for ch_id, info in result.items():
            _sessions_store[(ch_id, None)] = info
        return result
    except (FileNotFoundError, json.JSONDecodeError):
        return OrderedDict()


def save_sessions(s: OrderedDict[int, SessionInfo], *, _path: str | None = None) -> None:
    target = _path or SESSIONS_PATH
    tmp = target + ".tmp"
    data = {
        str(k): {
            "session_id": v.session_id,
            "mcp_server_keys": v.mcp_server_keys,
        }
        for k, v in s.items()
    }
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_session(
    sessions: OrderedDict[int, SessionInfo],
    channel_id: int,
    info: SessionInfo,
    user_id: int | None = None,
) -> None:
    """Mutate the session dict in-place and schedule a non-blocking save.

    The dict mutation is synchronous (cheap, single hash lookup + LRU pop)
    but the disk write is offloaded to a worker thread via `asyncio.to_thread`
    so the event loop does not stall on every Discord message that produces
    a new SDK session ID. Falls back to a synchronous save when called
    outside an event loop (tests / CLI scripts).

    `user_id` is forwarded to `set_session` so the internal composite-key
    store is also updated. Pass None for backward-compatible behavior.
    """
    sessions[channel_id] = info
    sessions.move_to_end(channel_id)
    while len(sessions) > MAX_SESSIONS:
        sessions.popitem(last=False)
    # Mirror into composite-key store.
    set_session(channel_id, user_id, info)
    # set_session already schedules the save; no need to do it again.


def _on_save_done(task: asyncio.Task) -> None:
    """Discard the task ref and surface any save error to the logger.

    Without this, an exception from save_sessions (disk full, permission
    error, etc.) would be deferred to GC as "Task exception was never
    retrieved" — silent in unattended Discord bots.
    """
    _save_tasks.discard(task)
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        log.error("save_sessions failed: %s", exc, exc_info=(type(exc), exc, exc.__traceback__))


# ---------------------------------------------------------------------------
# JSONL conversation history (free-code: {sessionId}.jsonl)
# ---------------------------------------------------------------------------
_CHANNEL_ID_MIN = 10**14
_CHANNEL_ID_MAX = 10**19


def _validate_channel_id(channel_id: int) -> None:
    if not isinstance(channel_id, int) or not (_CHANNEL_ID_MIN <= channel_id <= _CHANNEL_ID_MAX):
        raise ValueError(f"invalid channel_id range: {channel_id!r}")


def _history_file(channel_id: int) -> str:
    _validate_channel_id(channel_id)
    return os.path.join(HISTORY_DIR, f"{channel_id}.jsonl")


def append_jsonl(channel_id: int, entry: dict) -> None:
    """Append a single entry to the channel's JSONL transcript (fire-and-forget safe)."""
    _validate_channel_id(channel_id)
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
        f.flush()
        os.fsync(f.fileno())


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
            # `append` + single `reverse()` is strictly O(n) total. The
            # `insert(0, ...)` alternative is O(N) per call → O(N²) overall
            # (negligible at typical sizes but the cleaner idiom).
            chunks: list[bytes] = []
            lines_found = 0
            pos = size
            # Read chunks from the tail until we have enough newlines or hit BOF.
            while pos > 0 and lines_found <= max_entries * 2:
                read_size = min(chunk, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size)
                chunks.append(buf)
                # Count newlines in the new chunk only — total grows monotonically.
                lines_found += buf.count(b"\n")
            chunks.reverse()
            text = b"".join(chunks).decode("utf-8", errors="replace")
            # If we stopped before BOF, the first decoded "line" is almost
            # always a partial record left over from where the chunk boundary
            # bisected an entry. Trying to json.loads() that half-record was
            # the source of the steady stream of "Skipping corrupted JSONL
            # line: Expecting value" warnings on every load. Drop it.
            reached_bof = (pos == 0)
    except OSError:
        return []

    lines = text.splitlines()
    if lines and not reached_bof:
        lines = lines[1:]

    messages: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("Skipping corrupted JSONL line in %s: %s", path, e)
            continue
        if entry.get("role") in ("user", "assistant"):
            messages.append({"role": entry["role"], "content": entry["content"]})
    if len(messages) > max_entries:
        messages = messages[-max_entries:]
    return messages


def load_history_channel(channel_id: int) -> list[dict]:
    """Load recent conversation messages for a channel (tail only)."""
    _validate_channel_id(channel_id)
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

"""Session and conversation persistence — matches free-code utils/sessionStorage.ts pattern.

JSONL append-only transcripts per channel, JSON for SDK session IDs.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from config import (
    COMPACT_HISTORY_ASSISTANT_CHARS,
    COMPACT_HISTORY_CONTEXT_SNAPSHOT_CHARS,
    COMPACT_HISTORY_TOOL_RESULT_CHARS,
    COMPACT_HISTORY_USER_CHARS,
    HISTORY_DIR,
    MAX_MESSAGES_PER_CHANNEL,
    MAX_SESSIONS,
    SESSIONS_PATH,
)
from utils.atomic_write import atomic_write_json

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
    # 機能6: last turn's usage breakdown (input/cache_read/cache_creation/output
    # tokens). Surfaced to /context so the admin can see "how close are we to
    # the 500K compaction trigger". Not persisted to sessions.json — the disk
    # serializer keeps the legacy {session_id, mcp_server_keys} contract.
    last_usage: dict | None = None


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


def clear_all_sessions_for_channel(channel_id: int) -> int:
    """Drop every (channel_id, *) session. Returns the number dropped."""
    drop = [k for k in _sessions_store if k[0] == channel_id]
    for k in drop:
        _sessions_store.pop(k, None)
    if drop:
        _schedule_save()
    return len(drop)


def clear_channel_context(channel_id: int) -> dict:
    """Clear the channel's conversation context.

    Two effects (both required for a true "fresh start"):
      1. Rename the JSONL transcript to ``{channel_id}.jsonl.cleared-<ts>``
         so replay-history load sees no prior turns. NOT deleted — kept for
         audit / rollback (mv is atomic on the same filesystem, replay uses
         only ``{channel_id}.jsonl``).
      2. Drop every in-memory session keyed on this channel_id so the next
         turn takes the cold-start path (no session_id, no resume flag).

    Returns a small report dict for the caller to surface:
      {archived_path: str|None, bytes: int, sessions_dropped: int}
    """
    _validate_channel_id(channel_id)
    report = {"archived_path": None, "bytes": 0, "sessions_dropped": 0}
    src = _history_file(channel_id)
    try:
        st = os.stat(src)
        report["bytes"] = st.st_size
        ts = time.strftime("%Y%m%dT%H%M%S")
        dst = f"{src}.cleared-{ts}"
        os.rename(src, dst)
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
        report["archived_path"] = dst
    except FileNotFoundError:
        pass   # 履歴が無いチャンネル: session drop だけやる
    report["sessions_dropped"] = clear_all_sessions_for_channel(channel_id)
    return report


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
    data = {
        str(k): {
            "session_id": v.session_id,
            "mcp_server_keys": v.mcp_server_keys,
        }
        for k, v in s.items()
    }
    atomic_write_json(target, data)


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

    # Mirror the line into the BM25 history index for per-turn literal recall.
    # Single chokepoint: every transcript write goes through append_jsonl, so
    # hooking here covers both the OAuth (entry.py) and API-key (__init__.py)
    # paths uniformly. Non-fatal — a transcript must persist even if indexing
    # fails. Caller already runs append_jsonl off the event loop via to_thread,
    # so the synchronous index write piggybacks on that thread.
    try:
        role = entry.get("role")
        content = entry.get("content")
        if role and content:
            from services.mem0 import history_index
            history_index.index_line(
                channel_id, entry.get("timestamp", ""), role, content,
                author=entry.get("author", ""),
            )
    except Exception as exc:
        log.warning("history index update failed (non-fatal): %s", exc)

    # Phase 5: also mirror into the semantic (vector) transcript index. The
    # env flag is checked BEFORE importing so the default-OFF path adds zero
    # work here (no import, no call) — append_jsonl is on the concurrent write
    # path and any extra latency widens the BM25 write race above. Own try so a
    # vector-store hiccup never affects the BM25 path.
    try:
        from config import MEM0_HISTORY_EMBED_ENABLED
        if MEM0_HISTORY_EMBED_ENABLED:
            role = entry.get("role")
            content = entry.get("content")
            if role and content:
                from services.mem0 import history_embed
                history_embed.index_line(
                    channel_id, entry.get("timestamp", ""), role,
                    entry.get("author", ""), content,
                )
    except Exception as exc:
        log.warning("history embed update failed (non-fatal): %s", exc)


_CONTEXT_BLOCK_RE = re.compile(
    r"<(?P<tag>retrieved_memories|kanae_self_memory)(?:\s[^>]*)?>.*?</(?P=tag)>",
    re.DOTALL,
)


def _compact_text(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[...truncated by compact history cap...]"


def extract_context_snapshot(
    prompt_text: str,
    limit: int = COMPACT_HISTORY_CONTEXT_SNAPSHOT_CHARS,
) -> str:
    """Pull the Mem0 / kanae_self_memory blocks that precede untrusted user input.

    Anything inside the ``<untrusted_user_input>`` wrapper is data, not
    recall context — skip it. The blocks before the wrapper are the
    host-injected recall snapshot we want to persist alongside the turn.
    """
    trusted_prefix = (prompt_text or "").split("<untrusted_user_input>", 1)[0]
    blocks = [m.group(0).strip() for m in _CONTEXT_BLOCK_RE.finditer(trusted_prefix)]
    return _compact_text("\n\n".join(blocks), limit) if blocks else ""


def prompt_has_context_snapshot(prompt_text: str) -> bool:
    return bool(extract_context_snapshot(prompt_text, limit=1))


def _clean_author_label(author: str) -> str:
    """Sanitize a speaker label for safe inline use in ``[発言者: ...]``.

    Strips newlines and the ``]`` that would close the attribution bracket, so
    a stored label can't break the prompt fence on replay. Labels come from the
    config SPEAKER_LABELS table (operator-controlled), so this is belt-and-braces.
    """
    return author.replace("\n", " ").replace("\r", " ").replace("]", "").strip()[:40]


def _cap_tool_result_content(content, limit: int):
    """Cap a tool_result block's ``content`` (str, or list of text/image blocks)."""
    if isinstance(content, str):
        return _compact_text(content, limit)
    if isinstance(content, list):
        out = []
        for b in content:
            if (
                isinstance(b, dict)
                and b.get("type") == "text"
                and isinstance(b.get("text"), str)
            ):
                nb = dict(b)
                nb["text"] = _compact_text(b["text"], limit)
                out.append(nb)
            else:
                out.append(b)
        return out
    return content


def _strip_display_text_tool_call(content) -> str:
    """Strip text-form tool-call XML from a flattened display string."""
    if not isinstance(content, str):
        return content if isinstance(content, str) else ""
    from services.claude_api.errors import strip_text_tool_call
    return strip_text_tool_call(content).strip()


def _strip_text_tool_calls_in_blocks(content: list) -> list:
    """Strip text-form tool-call XML from text blocks; drop emptied ones.

    A text block whose body is (or ends in) a plain-text ``<invoke …>`` /
    ``<function_calls>`` span is a non-executing tool-call artifact that
    poisons resume (the model mimics its own broken format). Stripping it
    here, at both the persist and replay chokepoints, cuts the source and
    decontaminates existing transcripts
    (docs/2026-06-13_tool_call_text_invoke_poison.md).
    """
    from services.claude_api.errors import (
        contains_text_tool_call,
        strip_text_tool_call,
    )
    out = []
    for b in content:
        if (isinstance(b, dict) and b.get("type") == "text"
                and contains_text_tool_call(b.get("text") or "")):
            cleaned = strip_text_tool_call(b["text"])
            if cleaned.strip():
                out.append({**b, "text": cleaned})
            # else: block was nothing but a text-form tool call → drop it
        else:
            out.append(b)
    return out


def _prepare_api_messages(
    turns: list[dict],
    tool_result_limit: int = COMPACT_HISTORY_TOOL_RESULT_CHARS,
) -> list[dict]:
    """Return a JSON-safe, tool_result-capped copy of structured turns.

    ``turns`` are the assistant + tool_result messages the agent loop appended
    to its in-memory history (already plain block-dicts via ``_block_to_dict``).
    We copy defensively so the live in-memory history is never mutated, and cap
    each tool_result payload so a huge Bash dump / file read does not bloat every
    future replay. thinking ``signature`` and tool_use ``id``/``input`` are kept
    verbatim — they are load-bearing for the resumed request. Text-form tool
    calls are stripped so they never enter the transcript (write-time poison
    guard, 2026-06-13).
    """
    prepared: list[dict] = []
    for m in turns:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            new_content = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    nb = dict(b)
                    nb["content"] = _cap_tool_result_content(
                        b.get("content"), tool_result_limit
                    )
                    new_content.append(nb)
                else:
                    new_content.append(b)
            new_content = _strip_text_tool_calls_in_blocks(new_content)
            if not new_content:
                # Whole turn was a text-form tool call artifact — drop it so
                # it is never persisted (would otherwise poison future resumes).
                continue
            prepared.append({"role": role, "content": new_content})
        else:
            prepared.append({"role": role, "content": content})
    return prepared


def build_compact_history_entries(
    raw_user_text: str,
    prompt_text: str,
    assistant_text: str,
    *,
    previous_context_snapshot: str = "",
    author: str = "",
    api_messages: list[dict] | None = None,
) -> tuple[dict, dict]:
    """Build the user+assistant JSONL pair without re-saving the full prompt.

    The historic JSONL stored ``prompt_text`` verbatim, which meant every
    replay re-sent Mem0, kanae_state, kanae_self_memory, the trusted
    header, and the untrusted wrapper. Compact storage drops all of that
    and keeps only the raw user message and assistant reply, plus a
    ``context_snapshot`` field that carries the latest Mem0 blocks for
    short follow-ups. The snapshot is replayed by ``load_api_replay_channel``
    only; the web history view ignores it.

    ``author`` (stable speaker label) is stamped onto the user entry so a
    later resume can attribute past turns when multiple admins share a channel.
    Empty string = no attribution recorded (back-compat: old rows have no author).

    ``api_messages`` (optional) is the agent loop's full structured expansion
    for THIS request — the assistant thinking(+signature)/tool_use turns and
    their tool_result turns. When supplied it is attached to the assistant entry
    so ``load_api_replay_channel`` can replay a lossless, CLI-shaped transcript
    instead of the flattened ``content`` text (the structureless replay was the
    cause of the resume refusals). The ``content`` field stays a display string
    either way, so the web view / BM25 index are unaffected, and old rows lacking
    this field replay via ``content`` (back-compat). Because each request's whole
    tool_use/tool_result expansion lives inside one assistant row, tail
    truncation can never split a tool_use from its tool_result.
    """
    snapshot = extract_context_snapshot(prompt_text) or previous_context_snapshot
    user_entry: dict = {
        "role": "user",
        "content": _compact_text(raw_user_text or "", COMPACT_HISTORY_USER_CHARS),
    }
    cleaned_author = _clean_author_label(author) if author else ""
    if cleaned_author:
        user_entry["author"] = cleaned_author
    if snapshot:
        user_entry["context_snapshot"] = _compact_text(
            snapshot, COMPACT_HISTORY_CONTEXT_SNAPSHOT_CHARS
        )
    # Strip any text-form tool-call XML from the display string too, so the
    # web view / BM25 index / flattened-replay fallback never carry poison.
    # If the reply was nothing but a text-form call, the display string ends
    # up empty — fine, the (cleaned) api_messages is the load-bearing replay.
    from services.claude_api.errors import contains_text_tool_call
    assistant_display = assistant_text or ""
    if contains_text_tool_call(assistant_display):
        assistant_display = _strip_display_text_tool_call(assistant_display)
    assistant_entry: dict = {
        "role": "assistant",
        "content": _compact_text(assistant_display, COMPACT_HISTORY_ASSISTANT_CHARS),
    }
    if api_messages:
        prepared = _prepare_api_messages(api_messages)
        if prepared:
            assistant_entry["api_messages"] = prepared
    return user_entry, assistant_entry


def _tail_jsonl(
    path: str,
    max_entries: int,
    *,
    include_context_snapshot: bool = False,
    max_age_days: int | None = None,
) -> list[dict]:
    """Read the last `max_entries` valid role='user'/'assistant' rows from a JSONL file.

    When ``max_age_days`` is set, entries whose ``timestamp`` is older than
    that many days are silently dropped.

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

    cutoff_dt = None
    if max_age_days is not None:
        from datetime import datetime, timezone, timedelta
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=max_age_days)

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
            if cutoff_dt is not None:
                ts_str = entry.get("timestamp", "")
                if ts_str:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        ts = _dt.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=_tz.utc)
                        if ts < cutoff_dt:
                            continue
                    except (ValueError, TypeError):
                        pass
            msg = {"role": entry["role"], "content": entry["content"]}
            # Carry the speaker label forward when present so callers that care
            # about attribution (load_api_replay_channel) can use it. Absent on
            # old rows / assistant turns — harmless to omit.
            if isinstance(entry.get("author"), str) and entry["author"]:
                msg["author"] = entry["author"]
            # Carry the lossless structured replay transcript (CLI-parity) so
            # load_api_replay_channel can expand it. Absent on old/display-only
            # rows — those replay via the flattened `content` string.
            if isinstance(entry.get("api_messages"), list):
                msg["api_messages"] = entry["api_messages"]
            # Carry the compact-summary marker (ADR-0010 phase 3) so
            # _apply_compact_summary can collapse the replay window at it.
            if entry.get("is_compact_summary") is True:
                msg["is_compact_summary"] = True
                if isinstance(entry.get("keep_tail_rows"), int):
                    msg["keep_tail_rows"] = entry["keep_tail_rows"]
            if include_context_snapshot and isinstance(entry.get("context_snapshot"), str):
                msg["context_snapshot"] = entry["context_snapshot"]
                if entry.get("timestamp"):
                    msg["timestamp"] = entry["timestamp"]
            messages.append(msg)
    if len(messages) > max_entries:
        messages = messages[-max_entries:]
    return messages


def load_history_channel(channel_id: int) -> list[dict]:
    """Load recent conversation messages for a channel (tail only)."""
    from config import HISTORY_MAX_AGE_DAYS
    _validate_channel_id(channel_id)
    return _tail_jsonl(
        _history_file(channel_id),
        MAX_MESSAGES_PER_CHANNEL * 2,
        max_age_days=HISTORY_MAX_AGE_DAYS,
    )


def _sanitize_replay_messages(api_messages: list) -> list[dict]:
    """Mirror the official CLI's transcript restoration (binary fn DKf + Wt6).

    For every assistant/user message whose content is a block list:
      - drop ``text`` blocks whose ``text`` is not a string (interrupted-stream
        artifacts — the CLI logs "dropped non-string text block(s)");
      - if that leaves the content empty, drop the whole message
        (``Array.isArray(J) && J.length===0 -> return []``);
      - otherwise keep the (possibly trimmed) message.
    Messages whose content is not a list are passed through untouched
    (DKf returns null → original kept).
    """
    out: list[dict] = []
    for m in api_messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        filtered = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") == "text"
                    and not isinstance(b.get("text"), str))
        ]
        # Also strip text-form tool-call XML poison (2026-06-13) from the
        # surviving text blocks, decontaminating existing transcripts on replay.
        filtered = _strip_text_tool_calls_in_blocks(filtered)
        if not filtered:
            # Sanitized down to nothing → interrupted / poison frame, drop it.
            continue
        # CLI parity (binary fn WS$, audit #15): an assistant frame whose
        # surviving blocks are ALL thinking/redacted_thinking carries no
        # replayable content — replaying it standalone induces 400s /
        # regression loops on Fable. Drop the whole frame.
        if m.get("role") == "assistant" and all(
            isinstance(b, dict)
            and b.get("type") in ("thinking", "redacted_thinking")
            for b in filtered
        ):
            continue
        if filtered == content:
            out.append(m)  # unchanged
        else:
            out.append({**m, "content": filtered})
    return out


def _prune_orphan_tool_blocks(messages: list[dict]) -> list[dict]:
    """Final pairing sweep over the assembled replay window (binary fn Yy8).

    The Messages API hard-400s when a tool_use has no tool_result in the next
    user message (and vice versa). Our per-row sanitization can break pairing —
    e.g. a tool_result-only frame stripped to nothing is dropped, orphaning the
    tool_use on the assistant frame before it. This sweep runs LAST, on the
    final message list, and drops:

      1. assistant tool_use blocks whose id has no tool_result anywhere, and
      2. user tool_result blocks whose tool_use_id has no surviving tool_use.

    A message emptied by either pass is dropped whole. Pure function — the
    JSONL on disk is untouched (audit #2; the last remaining hard-400 resume
    path).
    """
    result_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    result_ids.add(b.get("tool_use_id", ""))

    surviving_use_ids: set[str] = set()
    pass1: list[dict] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") != "assistant" or not isinstance(content, list):
            pass1.append(m)
            continue
        kept = []
        for b in content:
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("id", "") not in result_ids
            ):
                log.warning(
                    "replay sweep: dropped orphan tool_use id=%s name=%s",
                    b.get("id", ""), b.get("name", ""),
                )
                continue
            kept.append(b)
        if not kept:
            continue
        for b in kept:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                surviving_use_ids.add(b.get("id", ""))
        pass1.append(m if kept == content else {**m, "content": kept})

    out: list[dict] = []
    for m in pass1:
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, list):
            out.append(m)
            continue
        kept = []
        for b in content:
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and b.get("tool_use_id", "") not in surviving_use_ids
            ):
                log.warning(
                    "replay sweep: dropped orphan tool_result tool_use_id=%s",
                    b.get("tool_use_id", ""),
                )
                continue
            kept.append(b)
        if not kept:
            continue
        out.append(m if kept == content else {**m, "content": kept})
    return out


def _apply_compact_summary(rows: list[dict]) -> list[dict]:
    """Collapse the replay window at the latest compact-summary row (ADR-0010).

    A compact-summary row (``is_compact_summary``) is a plain user row whose
    content is the official-CLI style continuation summary of everything
    before it. ``keep_tail_rows`` says how many rows immediately before it
    are preserved verbatim (the "Recent messages are preserved verbatim" leg
    of the CLI compact). Rows older than that boundary are dropped from the
    replay view only — they stay in the JSONL for audit / Read access.
    """
    last = None
    for i, row in enumerate(rows):
        if row.get("is_compact_summary"):
            last = i
    if last is None:
        return rows
    srow = rows[last]
    keep = srow.get("keep_tail_rows")
    keep = keep if isinstance(keep, int) and keep > 0 else 0
    keep_start = max(0, last - keep)
    return [srow] + rows[keep_start:last] + rows[last + 1:]


def history_file_path(channel_id: int) -> str:
    """Public path of the channel's JSONL transcript.

    Used by compaction to point the continuation summary at the full
    transcript ("read the full transcript at: …"), which かなえ can open
    with the Read tool when pre-compaction details are needed.
    """
    _validate_channel_id(channel_id)
    return _history_file(channel_id)


def load_compaction_rows(channel_id: int) -> list[dict]:
    """Row-level replay window (compact-summary applied) for compaction."""
    from config import HISTORY_MAX_AGE_DAYS
    _validate_channel_id(channel_id)
    rows = _tail_jsonl(
        _history_file(channel_id),
        MAX_MESSAGES_PER_CHANNEL * 2,
        max_age_days=HISTORY_MAX_AGE_DAYS,
    )
    return _apply_compact_summary(rows)


def load_api_replay_channel(channel_id: int) -> tuple[list[dict], str, str]:
    """Variant of load_history_channel for the API path.

    Returns ``(messages, latest_snapshot, snapshot_timestamp)`` where
    ``messages`` is the same role/content list as
    ``load_history_channel``, ``latest_snapshot`` is the most recent
    ``context_snapshot`` recorded across the tail (or "" if none), and
    ``snapshot_timestamp`` is the ISO-8601 timestamp of the JSONL row
    that carried that snapshot (or "" if unknown). The caller
    (``entry.py``) uses the timestamp to drop stale snapshots.
    """
    from config import HISTORY_MAX_AGE_DAYS
    _validate_channel_id(channel_id)
    rows = _tail_jsonl(
        _history_file(channel_id),
        MAX_MESSAGES_PER_CHANNEL * 2,
        include_context_snapshot=True,
        max_age_days=HISTORY_MAX_AGE_DAYS,
    )
    # Self-heal channels already poisoned by persisted sentinel/error replies:
    # drop assistant rows whose content is "(no response)" or an error message.
    # Replaying them primes the model to keep refusing (ADR-0008). New writes are
    # already filtered in entry.py; this cleans pre-existing history at read time
    # without mutating the JSONL on disk. Lazy import avoids a layering cycle.
    from services.claude_api.errors import is_non_persistable_reply

    latest_snapshot = ""
    latest_snapshot_ts = ""
    # Track the most recent Mem0 snapshot across the FULL tail (a snapshot can
    # sit on any row) before trimming the head for replay validity.
    for row in rows:
        if row.get("context_snapshot"):
            latest_snapshot = row["context_snapshot"]
            latest_snapshot_ts = row.get("timestamp", "")

    # Collapse the window at the latest compact-summary row (ADR-0010): the
    # summary replays as a normal user turn carrying the continuation
    # preamble; rows it covers are dropped from the view.
    rows = _apply_compact_summary(rows)

    # The replayed window MUST begin with a clean user turn. The Messages API
    # rejects a leading role=assistant message, and an assistant row's
    # api_messages expansion starts with an assistant message (its tool_result
    # turns are user messages INSIDE the expansion, which would be orphaned if
    # they led the window). Tail truncation can land mid-conversation on an
    # assistant row, so drop leading assistant rows until the first row is a
    # top-level user turn (always a plain string in our format).
    start = 0
    while start < len(rows) and rows[start].get("role") != "user":
        start += 1
    rows = rows[start:]

    messages: list[dict] = []
    for row in rows:
        if row["role"] == "assistant":
            # Self-heal: never replay a persisted sentinel/error reply — it
            # primes the model to keep refusing (ADR-0008). Skipping the row
            # drops its api_messages too, so no tool_result is orphaned.
            if is_non_persistable_reply(row.get("content")):
                continue
            api_messages = row.get("api_messages")
            if isinstance(api_messages, list) and api_messages:
                # Lossless CLI-shaped replay: splice this request's full
                # assistant thinking(+signature)/tool_use + tool_result
                # expansion in verbatim. The whole tool_use/tool_result chain is
                # self-contained in this one row, so pairs are never split.
                # Sanitize exactly like the official CLI's deserializeMessages
                # (strip non-string text blocks, drop frames left empty) so an
                # interrupted-stream artifact can't desync the tool_use↔
                # tool_result pairing the Messages API enforces.
                sanitized = _sanitize_replay_messages(api_messages)
                if sanitized:
                    messages.extend(sanitized)
                else:
                    # Everything was an artifact — fall back to the flattened
                    # display text (also poison-stripped) so the turn isn't
                    # lost entirely, unless it too was nothing but XML.
                    disp = _strip_display_text_tool_call(row.get("content"))
                    if disp:
                        messages.append({"role": "assistant", "content": disp})
            else:
                # Old / display-only row: replay the flattened assistant text,
                # with any text-form tool-call poison stripped.
                disp = _strip_display_text_tool_call(row.get("content"))
                if disp:
                    messages.append({"role": "assistant", "content": disp})
            continue
        # user row — attribute past turns to their speaker so resume can tell
        # apart multiple admins sharing a channel. The Messages API has no author
        # field on the user role, so we prepend it into the content. Deterministic
        # so the cached prefix stays stable; only top-level user turns (plain
        # strings) get the prefix — tool_result turns live inside api_messages.
        content = row["content"]
        author = row.get("author")
        if isinstance(author, str) and author:
            content = f"[発言者: {author}]\n{content}"
        messages.append({"role": "user", "content": content})
    # Final Yy8-style pairing sweep over the assembled window (audit #2):
    # whatever the per-row sanitization dropped, no tool_use may reach the
    # API without its tool_result and vice versa.
    messages = _prune_orphan_tool_blocks(messages)
    return messages, latest_snapshot, latest_snapshot_ts


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

"""JSONL event parser & filter.

Converts a raw event dict (one parsed JSONL line) into zero or more
SyncEvent instances. Never raises — malformed events return [].
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from services.vscode_sync.redactor import redact

SyncEventKind = Literal[
    "user_prompt",
    "tool_call",
    "tool_error",
    "assistant_text",
    "noise",
]

_NOISE_TOOLS = frozenset({"Read", "Glob", "Grep", "TodoWrite", "ListMcpResourcesTool"})
_LOUD_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"})

_MAX_TEXT_CHARS = 1500
_MAX_USER_PROMPT_CHARS = 500


@dataclass
class SyncEvent:
    kind: SyncEventKind
    session_id: str
    cwd: str
    git_branch: str | None
    timestamp: datetime
    payload: dict[str, Any] = field(default_factory=dict)


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return datetime.now(timezone.utc)


def _is_vscode_event(raw: dict[str, Any]) -> bool:
    return raw.get("entrypoint") == "claude-vscode"


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_tool_input(name: str, tool_input: dict[str, Any]) -> str:
    if name == "Bash":
        cmd = redact(str(tool_input.get("command", "")))
        return _truncate(cmd, 400)
    if name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "?")
        return path
    if name.startswith("mcp__"):
        keys = sorted(k for k in tool_input.keys() if isinstance(k, str))
        return "(" + ", ".join(keys) + ")"
    keys = sorted(k for k in tool_input.keys() if isinstance(k, str))
    return "(" + ", ".join(keys) + ")" if keys else ""


def _extract_user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                v = block.get("text")
                if isinstance(v, str):
                    texts.append(v)
        return "\n".join(texts) if texts else None
    return None


def _iter_tool_results(content: Any):
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        c = block.get("content")
        if isinstance(c, list):
            parts = []
            for sub in c:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    v = sub.get("text")
                    if isinstance(v, str):
                        parts.append(v)
            text = "\n".join(parts)
        elif isinstance(c, str):
            text = c
        else:
            text = ""
        yield block.get("tool_use_id"), text, bool(block.get("is_error"))


def parse_event(raw: dict[str, Any]) -> list[SyncEvent]:
    """Parse one JSONL event dict. Returns 0..N SyncEvents. Never raises."""
    try:
        if not isinstance(raw, dict):
            return []
        if not _is_vscode_event(raw):
            return []

        event_type = raw.get("type")
        if event_type in {
            "queue-operation",
            "ai-title",
            "last-prompt",
            "attachment",
            "file-history-snapshot",
            "system",
            "summary",
        }:
            return []

        session_id = raw.get("sessionId") or ""
        cwd = raw.get("cwd") or ""
        git_branch = raw.get("gitBranch")
        ts = _parse_ts(raw.get("timestamp"))

        if not session_id:
            return []

        events: list[SyncEvent] = []

        if event_type == "user":
            message = raw.get("message") or {}
            content = message.get("content")

            for _tool_use_id, text, is_error in _iter_tool_results(content):
                if is_error:
                    redacted = redact(_truncate(text, _MAX_TEXT_CHARS))
                    events.append(SyncEvent(
                        kind="tool_error",
                        session_id=session_id,
                        cwd=cwd,
                        git_branch=git_branch,
                        timestamp=ts,
                        payload={"text": redacted},
                    ))

            user_text = _extract_user_text(content)
            if user_text is not None and user_text.strip():
                redacted = redact(_truncate(user_text, _MAX_USER_PROMPT_CHARS))
                events.append(SyncEvent(
                    kind="user_prompt",
                    session_id=session_id,
                    cwd=cwd,
                    git_branch=git_branch,
                    timestamp=ts,
                    payload={"text": redacted},
                ))
            return events

        if event_type == "assistant":
            message = raw.get("message") or {}
            content = message.get("content")
            stop_reason = message.get("stop_reason")
            is_final_turn = stop_reason in {"end_turn", "stop_sequence"}

            if not isinstance(content, list):
                return []

            text_blocks: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "thinking":
                    events.append(SyncEvent(
                        kind="noise",
                        session_id=session_id,
                        cwd=cwd,
                        git_branch=git_branch,
                        timestamp=ts,
                        payload={"label": "Thinking"},
                    ))

                elif btype == "tool_use":
                    name = block.get("name") or "?"
                    tool_input = block.get("input") or {}
                    if name in _NOISE_TOOLS:
                        events.append(SyncEvent(
                            kind="noise",
                            session_id=session_id,
                            cwd=cwd,
                            git_branch=git_branch,
                            timestamp=ts,
                            payload={"label": name},
                        ))
                    else:
                        summary = _format_tool_input(name, tool_input)
                        events.append(SyncEvent(
                            kind="tool_call",
                            session_id=session_id,
                            cwd=cwd,
                            git_branch=git_branch,
                            timestamp=ts,
                            payload={"name": name, "summary": summary},
                        ))

                elif btype == "text":
                    v = block.get("text")
                    if isinstance(v, str) and v.strip():
                        text_blocks.append(v)

            if text_blocks:
                joined = "\n".join(text_blocks)
                if is_final_turn:
                    redacted = redact(_truncate(joined, _MAX_TEXT_CHARS))
                    events.append(SyncEvent(
                        kind="assistant_text",
                        session_id=session_id,
                        cwd=cwd,
                        git_branch=git_branch,
                        timestamp=ts,
                        payload={"text": redacted},
                    ))
                else:
                    events.append(SyncEvent(
                        kind="noise",
                        session_id=session_id,
                        cwd=cwd,
                        git_branch=git_branch,
                        timestamp=ts,
                        payload={"label": "text"},
                    ))
            return events

        return []
    except (KeyError, TypeError, AttributeError, ValueError):
        return []

"""Event aggregator — collapses bursts of events into a small number of
Discord messages.

`noise` events are collapsed per-label (e.g. "🔍 Read x5, Grep x3").
"""

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Awaitable, Callable

from services.vscode_sync.parser import SyncEvent

_MAX_CHUNK_CHARS = 1800

_NOISE_GLYPH = {
    "Read": "🔍",
    "Glob": "🗂",
    "Grep": "🔎",
    "Thinking": "🧠",
    "TodoWrite": "📝",
    "ListMcpResourcesTool": "🔌",
    "text": "💬",
}


@dataclass
class _SessionBuffer:
    cwd: str
    git_branch: str | None
    user_prompts: list[str]
    noises: Counter
    tool_calls: list[tuple[str, str]]
    tool_errors: list[str]
    assistant_texts: list[str]
    session_started: bool


def _format_header(session_id: str, cwd: str, git_branch: str | None) -> str:
    short = session_id[:8]
    branch = f" ({git_branch})" if git_branch else ""
    return f"-# ▶ `{short}` @ `{cwd}`{branch}"


def _format_noise(noises: Counter) -> str:
    parts = []
    for label, count in noises.most_common():
        glyph = _NOISE_GLYPH.get(label, "•")
        parts.append(f"{glyph} {label} x{count}")
    return ", ".join(parts)


def _split_chunks(text: str, limit: int = _MAX_CHUNK_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if cur and cur_len + line_len > limit:
            out.append("\n".join(cur))
            cur, cur_len = [], 0
        if line_len > limit:
            for i in range(0, len(line), limit):
                out.append(line[i : i + limit])
            continue
        cur.append(line)
        cur_len += line_len
    if cur:
        out.append("\n".join(cur))
    return out


class EventAggregator:
    def __init__(
        self,
        flush_callback: Callable[[str], Awaitable[None]],
        flush_interval: float = 2.0,
    ):
        self._flush = flush_callback
        self._flush_interval = flush_interval
        self._buffers: dict[str, _SessionBuffer] = {}
        self._sessions_seen: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def push(self, event: SyncEvent) -> None:
        buf = self._buffers.get(event.session_id)
        if buf is None:
            buf = _SessionBuffer(
                cwd=event.cwd,
                git_branch=event.git_branch,
                user_prompts=[],
                noises=Counter(),
                tool_calls=[],
                tool_errors=[],
                assistant_texts=[],
                session_started=event.session_id in self._sessions_seen,
            )
            self._buffers[event.session_id] = buf
            self._sessions_seen.add(event.session_id)
        else:
            buf.cwd = event.cwd
            buf.git_branch = event.git_branch

        if event.kind == "user_prompt":
            buf.user_prompts.append(event.payload.get("text", ""))
        elif event.kind == "noise":
            label = event.payload.get("label", "noise")
            buf.noises[label] += 1
        elif event.kind == "tool_call":
            buf.tool_calls.append((event.payload.get("name", "?"), event.payload.get("summary", "")))
        elif event.kind == "tool_error":
            buf.tool_errors.append(event.payload.get("text", ""))
        elif event.kind == "assistant_text":
            buf.assistant_texts.append(event.payload.get("text", ""))

        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._flush_interval)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._do_flush()

    async def _do_flush(self) -> None:
        if not self._buffers:
            return
        sessions = list(self._buffers.items())
        self._buffers.clear()

        for session_id, buf in sessions:
            lines: list[str] = []
            if not buf.session_started:
                lines.append(_format_header(session_id, buf.cwd, buf.git_branch))

            for prompt in buf.user_prompts:
                lines.append(f"👤 {prompt}")
            if buf.noises:
                lines.append(_format_noise(buf.noises))
            for name, summary in buf.tool_calls:
                if name == "Bash":
                    lines.append(f"🔧 `Bash`: `{summary}`" if summary else "🔧 `Bash`")
                elif name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
                    lines.append(f"✏️ `{name}` {summary}")
                elif name.startswith("mcp__"):
                    lines.append(f"🔌 `{name}` {summary}")
                else:
                    lines.append(f"🔧 `{name}` {summary}")
            for err in buf.tool_errors:
                lines.append("❌ tool_error:\n```\n" + err[:1200] + "\n```")
            for text in buf.assistant_texts:
                lines.append(f"💬 {text}")

            if not lines:
                continue

            content = "\n".join(lines)
            for chunk in _split_chunks(content):
                try:
                    await self._flush(chunk)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "vscode_sync flush callback raised"
                    )

    async def flush_now(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        async with self._lock:
            await self._do_flush()

    def mark_session_started(self, session_id: str) -> None:
        self._sessions_seen.add(session_id)

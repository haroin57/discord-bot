"""Time-based microcompact — a cheap, no-LLM token-reclaim layer.

Ported from the Claude Code CLI engine (agent-sdk 0.2.87 cli.js, functions
``$F_``/``wF_``, telemetry ``tengu_time_based_microcompact``). When a
conversation has been idle for longer than a threshold, the bulky bodies of
OLD ``tool_result`` blocks are replaced with a short placeholder while the
last ``keep_recent`` tool outputs are kept verbatim. Message order, count and
roles are never changed — only the heavy tool payloads shrink. This runs
BEFORE the expensive full LLM compaction, so most gaps never need a summary.
"""
from __future__ import annotations

PLACEHOLDER = "[Old tool result content cleared]"


def _tool_use_ids_in_order(messages: list[dict]) -> list[str]:
    """Collect tool_use ids from assistant turns, in conversation order."""
    ids: list[str] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") != "assistant" or not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                ids.append(b["id"])
    return ids


def _estimate_tokens(content) -> int:
    """Rough token estimate (~4 chars/token) of a tool_result body."""
    if isinstance(content, str):
        return max(1, len(content) // 4)
    # content can be a list of blocks (e.g. text/image) — size the text.
    try:
        return max(1, len(str(content)) // 4)
    except Exception:
        return 1


def microcompact_tool_results(
    messages: list[dict],
    *,
    gap_minutes: float,
    gap_threshold_min: float = 30.0,
    keep_recent: int = 3,
) -> tuple[list[dict], int]:
    """Clear old tool_result bodies when the idle gap exceeds the threshold.

    Returns ``(messages, tokens_saved)``. When the gap is below threshold, or
    there are no more than ``keep_recent`` tool calls, the input list is
    returned unchanged with ``tokens_saved == 0``.
    """
    if gap_minutes < gap_threshold_min:
        return messages, 0
    ids = _tool_use_ids_in_order(messages)
    if len(ids) <= keep_recent:
        return messages, 0
    keep = set(ids[-keep_recent:]) if keep_recent > 0 else set()

    saved = 0
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, list):
            out.append(m)
            continue
        changed = False
        new_content = []
        for b in content:
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and b.get("tool_use_id") not in keep
                and b.get("content") != PLACEHOLDER
            ):
                saved += _estimate_tokens(b.get("content"))
                new_content.append({**b, "content": PLACEHOLDER})
                changed = True
            else:
                new_content.append(b)
        out.append({**m, "content": new_content} if changed else m)
    return out, saved

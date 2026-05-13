"""Agentic tool-use loop driving the direct-API path.

One turn = stream a response from the API, observe ``tool_use`` blocks the
model emitted, gate them through ``can_use_tool``, execute approved ones,
package the results as ``tool_result`` blocks, and feed them back in the
next turn. Stops when the model emits ``end_turn`` / ``stop_sequence`` or
``max_turns`` is reached.

Differs from services/claude_sdk.py's ``call_with_tools`` (which delegates
to the ``claude`` CLI internally) by owning the loop ourselves: we see
every tool_use block, we route to either a builtin or MCP, and we synthesize
a session_id so SessionInfo round-trips cleanly even without a CLI session.

Notes:
- ``history`` is mutated in place; the caller (entry.py) is responsible for
  any disk persistence. tool_use / tool_result blocks are NOT written to
  JSONL (Codex v2 #5).
- ``api_client`` must be ``anthropic.AsyncAnthropic``; the sync client cannot
  yield streaming events.
"""

import logging
import time
import uuid
from dataclasses import dataclass

import anthropic

from config import CLAUDE_MAX_TOKENS

from services.claude_api.errors import classify_anthropic_error
from services.claude_api.mcp_client import kill_orphan_mcp_processes
from services.claude_api.stall import StallWatchdog
from services.claude_api.status import _safe_status, _tool_input_hint

log = logging.getLogger(__name__)


@dataclass
class AgentLoopResult:
    text: str
    synthetic_session_id: str | None
    active_mcp_keys: list[str]


def _build_cached_system(system_prompt: str) -> list[dict]:
    """Mirrors the helper in __init__.py — duplicated locally to dodge
    a circular import (services.claude_api.__init__ imports this module).
    """
    return [{"type": "text", "text": system_prompt,
             "cache_control": {"type": "ephemeral"}}]


def _inject_cache_breakpoints(messages: list[dict]) -> list[dict]:
    """Mirrors the helper in __init__.py (see _build_cached_system above)."""
    if len(messages) < 4:
        return messages
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) < 2:
        return messages
    target_idx = user_indices[-2]
    messages = list(messages)
    msg = dict(messages[target_idx])
    content = msg["content"]
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}},
        ]
    elif isinstance(content, list):
        content = list(content)
        last_block = dict(content[-1])
        last_block["cache_control"] = {"type": "ephemeral"}
        content[-1] = last_block
        msg["content"] = content
    messages[target_idx] = msg
    return messages


def _block_to_dict(block) -> dict:
    """Convert an anthropic content block (Pydantic model) to a serializable dict."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", None) or {},
        }
    if btype == "thinking":
        # Anthropic round-trips thinking content; keep it as-is for the next turn.
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", ""),
        }
    # Unknown / future block type — best effort.
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}


def _join_final_text(final_message) -> str:
    parts = []
    for block in getattr(final_message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


def _normalize_history_for_api(history: list[dict]) -> list[dict]:
    """Strip extra fields, leaving only {role, content} for the API."""
    return [
        {"role": h["role"], "content": h["content"]}
        for h in history
    ]


async def run_agent_loop(
    prompt: str,
    channel_id: int,
    *,
    history: list[dict],
    system_prompt: str,
    tools_for_api: list[dict],
    can_use_tool,  # (name, input) -> (bool, reason)
    execute_tool,  # async (name, input) -> str
    status_callback=None,
    max_turns: int,
    stall_watchdog: StallWatchdog,
    api_client: anthropic.AsyncAnthropic,
    model: str,
    extra_betas: list[str],
    extra_headers: dict | None = None,
    active_mcp_keys: list[str] | None = None,
) -> AgentLoopResult:
    """Run the streaming + tool-use loop. See module docstring for invariants."""
    history.append({"role": "user", "content": prompt})
    active_mcp_keys = list(active_mcp_keys or [])
    last_text = ""

    for turn in range(max_turns):
        cached_system = _build_cached_system(system_prompt)
        messages = _normalize_history_for_api(history)
        messages = _inject_cache_breakpoints(messages)

        stream_kwargs = dict(
            model=model,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=cached_system,
            messages=messages,
            betas=extra_betas,
        )
        if tools_for_api:
            stream_kwargs["tools"] = tools_for_api
        if extra_headers:
            stream_kwargs["extra_headers"] = extra_headers

        try:
            async with api_client.beta.messages.stream(**stream_kwargs) as stream:
                async for event in stream:
                    stall_watchdog.heartbeat()
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        btype = getattr(block, "type", None) if block else None
                        if btype == "thinking":
                            await _safe_status(status_callback, "💭 thinking…")
                        elif btype == "text":
                            await _safe_status(status_callback, "📝 応答を生成中")
                        elif btype == "tool_use":
                            name = getattr(block, "name", "?")
                            hint = _tool_input_hint(name, getattr(block, "input", None))
                            short_name = name.split("__", 2)[-1] if "__" in name else name
                            label = f"🔧 {short_name}"
                            if hint:
                                label += f" `{hint[:60]}`"
                            await _safe_status(status_callback, label)
                final_msg = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            category = classify_anthropic_error(exc)
            log.error("agent loop API error turn=%d category=%s: %s",
                      turn, category, exc)
            # Caller (entry.py) owns retry/backoff; surface the exception.
            raise

        # Persist the assistant turn (in-memory only; disk write is caller's job)
        assistant_blocks = [_block_to_dict(b) for b in (final_msg.content or [])]
        history.append({"role": "assistant", "content": assistant_blocks})

        stop_reason = getattr(final_msg, "stop_reason", None)
        if stop_reason in ("end_turn", "stop_sequence"):
            last_text = _join_final_text(final_msg)
            return AgentLoopResult(
                text=last_text,
                synthetic_session_id=uuid.uuid4().hex,
                active_mcp_keys=active_mcp_keys,
            )
        if stop_reason != "tool_use":
            # max_tokens / refusal / unexpected — return whatever text we have.
            last_text = _join_final_text(final_msg)
            return AgentLoopResult(
                text=last_text or "(no response)",
                synthetic_session_id=None,
                active_mcp_keys=active_mcp_keys,
            )

        # Execute every tool_use block in this turn.
        tool_results: list[dict] = []
        for block in final_msg.content or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_id = getattr(block, "id", "")
            tool_name = getattr(block, "name", "")
            tool_input = getattr(block, "input", None) or {}

            allowed, reason = can_use_tool(tool_name, tool_input)
            if not allowed:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Tool {tool_name} denied: {reason}",
                    "is_error": True,
                })
                await _safe_status(status_callback, "❌ ツール拒否")
                continue

            try:
                t0 = time.monotonic()
                output = await execute_tool(tool_name, tool_input)
                dt = time.monotonic() - t0
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": output if isinstance(output, str) else str(output),
                })
                await _safe_status(
                    status_callback,
                    f"📨 結果を受信 ({dt:.1f}s)",
                )
            except Exception as exc:
                log.warning("tool %s failed: %s", tool_name, exc)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Error: {exc}",
                    "is_error": True,
                })
                await _safe_status(status_callback, "❌ ツールエラー")

        history.append({"role": "user", "content": tool_results})

    # Reached max_turns.
    log.warning("agent loop exhausted max_turns=%d for ch=%d", max_turns, channel_id)
    return AgentLoopResult(
        text=last_text or "🔄 ターン上限に達しました。もう一度話しかけてください。",
        synthetic_session_id=None,
        active_mcp_keys=active_mcp_keys,
    )


def kill_orphan_claude_cli() -> int:
    """API-side cleanup: SIGTERM/SIGKILL MCP child processes.

    Replaces services/claude_sdk.py:kill_orphan_claude_cli for the direct-API
    path. The name is preserved for back-compat (main_async.py shutdown still
    imports this symbol).
    """
    return kill_orphan_mcp_processes()

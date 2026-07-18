"""Pure validation and one-tool execution primitives for the agent loop."""

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass

from config import TOOL_CALL_TIMEOUT_S
from services.claude_api.status import _safe_status


log = logging.getLogger("services.claude_api.tool_loop")


@dataclass
class _RescuedToolUse:
    """Duck-typed tool-use block created from a rescued text invocation."""

    id: str
    name: str
    input: dict
    type: str = "tool_use"


def _truncate_head_tail(text: str, cap: int) -> str:
    """Middle-out truncation that preserves the head and tail."""
    if cap <= 0 or len(text) <= cap:
        return text
    half = max(1, cap // 2)
    omitted = len(text) - cap
    return (
        text[:half]
        + f"\n...[{omitted} chars omitted by MCP_TOOL_RESULT_CHARS cap]...\n"
        + text[-half:]
    )


def _cap_mcp_result_content(
    content: "str | list[dict]",
    cap: int,
) -> "str | list[dict]":
    """Apply a character cap to text in an MCP result payload."""
    if cap <= 0:
        return content
    if isinstance(content, str):
        return _truncate_head_tail(content, cap)
    if isinstance(content, list):
        def _is_text(block: object) -> bool:
            return (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )

        text_count = sum(1 for block in content if _is_text(block))
        total = sum(len(block["text"]) for block in content if _is_text(block))
        if not text_count or total <= cap:
            return content
        per_block = max(1, cap // text_count)
        return [
            {**block, "text": _truncate_head_tail(block["text"], per_block)}
            if _is_text(block)
            else block
            for block in content
        ]
    return content


def _validate_tool_input(schema, args: dict) -> str | None:
    """Validate arguments against a tool input schema."""
    if not isinstance(schema, dict) or not schema:
        return None
    try:
        import jsonschema
    except ImportError:
        return None
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        location = f" at '{path}'" if path else ""
        return f"{exc.message}{location}"
    except jsonschema.SchemaError:
        return None
    return None


async def _run_one_tool_inner(
    block,
    *,
    can_use_tool,
    execute_tool,
    tool_schemas: dict,
    status_callback,
    tool_invocation_callback,
    turn: int,
    _tool_call_timeout_s: float = TOOL_CALL_TIMEOUT_S,
) -> dict:
    """Execute one tool-use block and return a tool-result dictionary."""
    tool_id = getattr(block, "id", "")
    tool_name = getattr(block, "name", "")
    tool_input = getattr(block, "input", None) or {}

    if tool_invocation_callback is not None:
        try:
            await tool_invocation_callback(tool_name, tool_input)
        except Exception as exc:
            log.warning(
                "tool_invocation_callback failed (tool=%s turn=%d): %s",
                tool_name,
                turn,
                exc,
            )

    validation_error = _validate_tool_input(tool_schemas.get(tool_name), tool_input)
    if validation_error is not None:
        log.info("tool %s input validation failed: %s", tool_name, validation_error)
        await _safe_status(status_callback, "❌ ツール入力エラー")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": (
                "<tool_use_error>InputValidationError: "
                f"{validation_error}</tool_use_error>"
            ),
            "is_error": True,
        }

    gate_result = can_use_tool(tool_name, tool_input)
    if inspect.isawaitable(gate_result):
        gate_result = await gate_result
    allowed, reason = gate_result
    if not allowed:
        await _safe_status(status_callback, "❌ ツール拒否")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": f"Tool {tool_name} denied: {reason}",
            "is_error": True,
        }

    try:
        started = time.monotonic()
        output = await asyncio.wait_for(
            execute_tool(tool_name, tool_input),
            timeout=_tool_call_timeout_s,
        )
        elapsed = time.monotonic() - started
        if isinstance(output, list):
            result_content: "str | list[dict]" = output
        elif isinstance(output, str):
            result_content = output
        else:
            result_content = str(output)
        await _safe_status(status_callback, f"📨 結果を受信 ({elapsed:.1f}s)")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": result_content,
        }
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("tool %s timed out after %ds", tool_name, _tool_call_timeout_s)
        await _safe_status(status_callback, "⏰ ツールがタイムアウト")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": (
                f"<tool_use_error>Tool {tool_name} timed out after "
                f"{_tool_call_timeout_s}s and was aborted.</tool_use_error>"
            ),
            "is_error": True,
        }
    except Exception as exc:
        log.warning("tool %s failed: %s", tool_name, exc)
        await _safe_status(status_callback, "❌ ツールエラー")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": f"<tool_use_error>{exc}</tool_use_error>",
            "is_error": True,
        }

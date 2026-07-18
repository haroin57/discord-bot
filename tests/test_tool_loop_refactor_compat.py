"""Compatibility gates for the responsibility split behind tool_loop.py."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.claude_api.loop_contracts as contracts
import services.claude_api.loop_observability as observability
import services.claude_api.message_codec as codec
import services.claude_api.request_shape as request_shape
import services.claude_api.tool_display as display
import services.claude_api.tool_execution as execution
import services.claude_api.tool_loop as tool_loop


def _assert_reexports(module, names: tuple[str, ...]) -> None:
    for name in names:
        assert getattr(tool_loop, name) is getattr(module, name), name


def _assert_value_reexports(module, names: tuple[str, ...]) -> None:
    for name in names:
        assert getattr(tool_loop, name) == getattr(module, name), name


def test_loop_contract_reexports_are_object_identical():
    _assert_reexports(contracts, (
        "AgentLoopResult",
        "_model_display",
        "_fallback_alert",
        "_build_checkpoint_result",
    ))
    _assert_value_reexports(contracts, (
        "_CHECKPOINT_NOTE",
        "_AUTO_RESUME_NOTE",
        "AUTO_RESUME_DIRECTIVE",
    ))


def test_request_shape_reexports_are_object_identical():
    _assert_reexports(request_shape, (
        "_build_cached_system",
        "_mark_cache_breakpoint",
        "_inject_cache_breakpoints",
        "get_attribution_header_value",
        "get_metadata",
        "CONTEXT_MANAGEMENT_CONFIG",
        "OUTPUT_CONFIG",
        "THINKING_CONFIG",
    ))
    _assert_value_reexports(request_shape, (
        "CLAUDE_MAX_TOKENS",
        "CLAUDE_MAX_TOKENS_OPUS",
        "CLI_DEFAULT_SYSTEM_PROMPT",
        "CLI_IDENTITY_TEXT",
        "REFUSAL_FALLBACK_HEADER",
    ))


def test_message_codec_reexports_are_object_identical():
    _assert_reexports(codec, (
        "_block_to_dict",
        "_response_blocks_to_dict",
        "_serialize_search_result_content",
        "_join_final_text",
        "_EMPTY_CONTENT_PLACEHOLDER",
        "_sanitize_content_blocks",
        "strip_tool_search_blocks",
        "_has_unpaired_server_tool_use",
        "_normalize_history_for_api",
    ))
    _assert_value_reexports(codec, (
        "_TOOL_SEARCH_BLOCK_TYPES",
        "_SERVER_TOOL_RESULT_TYPES",
    ))


def test_display_execution_and_observability_reexports_are_object_identical():
    _assert_reexports(display, (
        "_cap_inline",
        "_format_tool_invocation_for_display",
    ))
    _assert_value_reexports(display, (
        "_TOOL_DETAIL_INLINE_CAP",
        "_TOOL_DETAIL_FIELD_CAP",
    ))
    _assert_reexports(execution, (
        "_RescuedToolUse",
        "_truncate_head_tail",
        "_cap_mcp_result_content",
        "_validate_tool_input",
    ))
    _assert_reexports(observability, (
        "_HALLUC_VERB_RE",
        "_HALLUC_PATH_RE",
        "_detect_write_hallucination",
        "_usage_number",
        "_last_context_tokens",
        "get_last_context_tokens",
        "_context_tokens",
        "_log_turn_usage",
        "_log_total_usage",
        "_observe_response_headers",
    ))
    _assert_value_reexports(observability, (
        "_HALLUC_VERBS",
        "_HALLUC_MAX_DISTANCE",
        "_HALLUC_WRITE_TOOLS",
        "_USAGE_FIELDS",
    ))


def test_run_agent_loop_signature_is_stable():
    parameters = tuple(inspect.signature(tool_loop.run_agent_loop).parameters.values())
    assert tuple(parameter.name for parameter in parameters) == (
        "prompt", "channel_id", "history", "system_prompt", "tools_for_api",
        "can_use_tool", "execute_tool", "status_callback",
        "incremental_text_callback", "text_stream_callback",
        "tool_invocation_callback", "scratch_stream_callback",
        "scratch_finalize_callback", "thinking_stream_callback",
        "thinking_finalize_callback", "progress", "max_turns",
        "stall_watchdog", "api_client", "model", "extra_betas",
        "extra_headers", "active_mcp_keys", "refusal_fallback_model",
        "proactive_compact", "prior_turns", "output_config_override",
        "tool_search_enabled", "evidence", "budget_role", "request_budget",
        "loop_detector", "should_auto_resume",
    )
    assert tuple(parameter.kind for parameter in parameters) == (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        *(inspect.Parameter.KEYWORD_ONLY for _ in range(31)),
    )
    required = inspect.Parameter.empty
    assert tuple(parameter.default for parameter in parameters) == (
        required, required, required, required, required, required, required,
        None, None, None, None, None, None, None, None, None,
        required, required, required, required, required, None, None, None,
        None, 0, None, False, None, "admin", None, None, None,
    )


def test_server_fallback_config_patch_reaches_request_builder(monkeypatch):
    common = dict(
        current_model="claude-fable-5-20260701",
        cached_system=[],
        messages=[],
        extra_betas=[],
        output_config_override=None,
        refusal_fallback_model="claude-opus-4-8",
        tools_for_api=[],
        extra_headers=None,
        fallback_used=False,
    )
    monkeypatch.setattr(tool_loop, "SERVER_SIDE_FALLBACK_ENABLED", False)
    disabled = tool_loop._build_stream_kwargs(**common)
    assert "fallbacks" not in disabled["extra_body"]

    monkeypatch.setattr(tool_loop, "SERVER_SIDE_FALLBACK_ENABLED", True)
    enabled = tool_loop._build_stream_kwargs(**common)
    assert enabled["extra_body"]["fallbacks"] == [
        {"model": "claude-opus-4-8"},
    ]

    monkeypatch.setattr(tool_loop, "REFUSAL_FALLBACK_HEADER", "x-test-fallback")
    with_header = tool_loop._build_stream_kwargs(**{**common, "fallback_used": True})
    assert with_header["extra_headers"]["x-test-fallback"] == "true"
    assert "x-is-refusal-fallback" not in with_header["extra_headers"]


@pytest.mark.asyncio
async def test_mcp_result_cap_patch_reaches_tool_wrapper(monkeypatch):
    monkeypatch.setattr(tool_loop, "DELIB_DISTILL_ENABLED", False)
    monkeypatch.setattr(tool_loop, "MCP_TOOL_RESULT_CHARS", 10)
    block = SimpleNamespace(
        id="tool-1",
        name="mcp__example__search",
        input={"query": "test"},
    )
    result = await tool_loop._run_one_tool(
        block,
        can_use_tool=lambda *_: (True, ""),
        execute_tool=AsyncMock(return_value="abcdefghijklmnopqrstuvwxyz"),
        tool_schemas={},
        status_callback=None,
        tool_invocation_callback=None,
        turn=0,
    )
    assert result["tool_use_id"] == "tool-1"
    assert "chars omitted" in result["content"]


@pytest.mark.asyncio
async def test_tool_timeout_patch_reaches_execution_wrapper(monkeypatch):
    monkeypatch.setattr(tool_loop, "TOOL_CALL_TIMEOUT_S", 0.001)

    async def slow_tool(*_):
        await asyncio.sleep(0.05)

    result = await tool_loop._run_one_tool_inner(
        SimpleNamespace(id="tool-2", name="Read", input={}),
        can_use_tool=lambda *_: (True, ""),
        execute_tool=slow_tool,
        tool_schemas={},
        status_callback=None,
        tool_invocation_callback=None,
        turn=0,
    )
    assert result["is_error"] is True
    assert "timed out after 0.001s" in result["content"]


@pytest.mark.asyncio
async def test_tool_executor_callback_gate_and_execution_order():
    trace = []

    async def invocation(name, tool_input):
        trace.append(("invoke", name, tool_input))

    async def gate(name, tool_input):
        trace.append(("gate", name, tool_input))
        return True, ""

    async def execute(name, tool_input):
        trace.append(("execute", name, tool_input))
        return "file contents"

    tool_input = {"file_path": "/tmp/a"}
    result = await tool_loop._run_one_tool_inner(
        SimpleNamespace(id="tool-3", name="Read", input=tool_input),
        can_use_tool=gate,
        execute_tool=execute,
        tool_schemas={},
        status_callback=None,
        tool_invocation_callback=invocation,
        turn=0,
    )
    assert trace == [
        ("invoke", "Read", tool_input),
        ("gate", "Read", tool_input),
        ("execute", "Read", tool_input),
    ]
    assert result == {
        "type": "tool_result",
        "tool_use_id": "tool-3",
        "content": "file contents",
    }

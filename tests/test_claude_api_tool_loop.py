"""Tests for services.claude_api.tool_loop.run_agent_loop."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from services.claude_api.stall import StallWatchdog
from services.claude_api.tool_loop import (
    AgentLoopResult,
    _block_to_dict,
    _join_final_text,
    run_agent_loop,
)


def _block(type_, **kwargs):
    return SimpleNamespace(type=type_, **kwargs)


def _final_msg(blocks, stop_reason):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


class _FakeStream:
    """Minimal async-iterable stream that returns a preset final message."""

    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for ev in self._events:
            yield ev

    async def get_final_message(self):
        return self._final


def _make_client(*responses):
    """responses: list of (events, final_msg) tuples, one per turn."""
    iterator = iter(responses)
    client = MagicMock()
    def stream_factory(**_kwargs):
        events, final = next(iterator)
        return _FakeStream(events, final)
    client.beta.messages.stream = stream_factory
    return client


def _allow_all(name, inp):
    return True, ""


def _deny_all(name, inp):
    return False, "denied"


async def _noop_status(_msg):
    pass


@pytest.fixture
async def watchdog():
    async with StallWatchdog(
        channel_id=1, status_callback=None,
        stall_threshold_s=999, idle_timeout_s=999, poll_interval_s=10,
    ) as wd:
        yield wd


class TestBlockToDict:
    def test_text(self):
        out = _block_to_dict(_block("text", text="hi"))
        assert out == {"type": "text", "text": "hi"}

    def test_tool_use(self):
        out = _block_to_dict(_block("tool_use", id="t1", name="Read", input={"x": 1}))
        assert out["type"] == "tool_use"
        assert out["id"] == "t1"
        assert out["input"] == {"x": 1}

    def test_thinking(self):
        out = _block_to_dict(_block("thinking", thinking="...think..."))
        assert out["type"] == "thinking"
        assert out["thinking"] == "...think..."


class TestJoinFinalText:
    def test_concat_text_blocks(self):
        final = _final_msg(
            [_block("text", text="hello"), _block("text", text=" world")],
            "end_turn",
        )
        assert _join_final_text(final) == "hello world"

    def test_ignores_tool_use(self):
        final = _final_msg(
            [_block("tool_use", id="x", name="t", input={}), _block("text", text="ok")],
            "end_turn",
        )
        assert _join_final_text(final) == "ok"


class TestRunAgentLoop:
    @pytest.mark.asyncio
    async def test_single_turn_end_turn(self, watchdog):
        client = _make_client((
            [],
            _final_msg([_block("text", text="hello!")], "end_turn"),
        ))
        history: list[dict] = []
        result = await run_agent_loop(
            "hi", channel_id=1, history=history,
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all,
            execute_tool=AsyncMock(),
            status_callback=_noop_status,
            max_turns=5,
            stall_watchdog=watchdog,
            api_client=client,
            model="claude-opus-4-6",
            extra_betas=[],
        )
        assert isinstance(result, AgentLoopResult)
        assert result.text == "hello!"
        assert result.synthetic_session_id is not None
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_tool_use_then_end(self, watchdog):
        client = _make_client(
            (
                [],
                _final_msg(
                    [_block("tool_use", id="t1", name="Read", input={"f": "x"})],
                    "tool_use",
                ),
            ),
            (
                [],
                _final_msg([_block("text", text="done")], "end_turn"),
            ),
        )
        execute = AsyncMock(return_value="file content")
        history: list[dict] = []
        result = await run_agent_loop(
            "read x", channel_id=1, history=history,
            system_prompt="sys",
            tools_for_api=[{"name": "Read", "description": "", "input_schema": {}}],
            can_use_tool=_allow_all,
            execute_tool=execute,
            status_callback=_noop_status,
            max_turns=5,
            stall_watchdog=watchdog,
            api_client=client,
            model="claude-opus-4-6",
            extra_betas=[],
        )
        assert result.text == "done"
        execute.assert_awaited_once_with("Read", {"f": "x"})
        # history: user (initial), assistant (tool_use), user (tool_result),
        # assistant (final text)
        assert [h["role"] for h in history] == ["user", "assistant", "user", "assistant"]
        assert history[2]["content"][0]["type"] == "tool_result"
        assert history[2]["content"][0]["content"] == "file content"

    @pytest.mark.asyncio
    async def test_denied_tool_returns_error_result(self, watchdog):
        client = _make_client(
            (
                [],
                _final_msg(
                    [_block("tool_use", id="t1", name="Bash", input={"command": "rm -rf"})],
                    "tool_use",
                ),
            ),
            (
                [],
                _final_msg([_block("text", text="ok")], "end_turn"),
            ),
        )
        execute = AsyncMock()
        history: list[dict] = []
        await run_agent_loop(
            "danger", channel_id=1, history=history,
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_deny_all,
            execute_tool=execute,
            status_callback=_noop_status,
            max_turns=5,
            stall_watchdog=watchdog,
            api_client=client,
            model="claude-opus-4-6",
            extra_betas=[],
        )
        execute.assert_not_awaited()
        tool_result_msg = history[2]["content"][0]
        assert tool_result_msg["is_error"] is True
        assert "denied" in tool_result_msg["content"]

    @pytest.mark.asyncio
    async def test_tool_exception_returns_error_result(self, watchdog):
        client = _make_client(
            (
                [],
                _final_msg(
                    [_block("tool_use", id="t1", name="Read", input={})],
                    "tool_use",
                ),
            ),
            (
                [],
                _final_msg([_block("text", text="x")], "end_turn"),
            ),
        )
        execute = AsyncMock(side_effect=RuntimeError("disk gone"))
        history: list[dict] = []
        await run_agent_loop(
            "read", channel_id=1, history=history,
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all,
            execute_tool=execute,
            status_callback=_noop_status,
            max_turns=5,
            stall_watchdog=watchdog,
            api_client=client,
            model="claude-opus-4-6",
            extra_betas=[],
        )
        tr = history[2]["content"][0]
        assert tr["is_error"] is True
        assert "disk gone" in tr["content"]

    @pytest.mark.asyncio
    async def test_max_turns_exhausted(self, watchdog):
        # Each turn requests another tool; we never see end_turn.
        responses = []
        for _ in range(3):
            responses.append((
                [],
                _final_msg(
                    [_block("tool_use", id=f"t{_}", name="Read", input={})],
                    "tool_use",
                ),
            ))
        client = _make_client(*responses)
        execute = AsyncMock(return_value="x")
        history: list[dict] = []
        result = await run_agent_loop(
            "loop", channel_id=1, history=history,
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all,
            execute_tool=execute,
            status_callback=_noop_status,
            max_turns=3,
            stall_watchdog=watchdog,
            api_client=client,
            model="claude-opus-4-6",
            extra_betas=[],
        )
        # Hit turn cap → synthetic_session_id is None
        assert result.synthetic_session_id is None
        assert "ターン上限" in result.text

    @pytest.mark.asyncio
    async def test_unexpected_stop_reason_returns_text(self, watchdog):
        client = _make_client((
            [],
            _final_msg([_block("text", text="cut off")], "max_tokens"),
        ))
        history: list[dict] = []
        result = await run_agent_loop(
            "x", channel_id=1, history=history,
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all,
            execute_tool=AsyncMock(),
            status_callback=_noop_status,
            max_turns=5,
            stall_watchdog=watchdog,
            api_client=client,
            model="claude-opus-4-6",
            extra_betas=[],
        )
        assert result.text == "cut off"
        assert result.synthetic_session_id is None

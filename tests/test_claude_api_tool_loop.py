"""Tests for services.claude_api.tool_loop.run_agent_loop."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from services.claude_api.stall import StallWatchdog
from services.claude_api.tool_loop import (
    MAX_CONSECUTIVE_EMPTY_TURNS,
    AgentLoopResult,
    _block_to_dict,
    _detect_write_hallucination,
    _format_tool_invocation_for_display,
    _join_final_text,
    _normalize_history_for_api,
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


class TestEmptyContentSanitizer:
    """_normalize_history_for_api must never emit empty content to the API."""

    def test_empty_string_replaced(self):
        out = _normalize_history_for_api([{"role": "user", "content": ""}])
        assert out[0]["role"] == "user"
        assert isinstance(out[0]["content"], list) and out[0]["content"]

    def test_empty_list_replaced(self):
        out = _normalize_history_for_api([{"role": "user", "content": []}])
        assert out[0]["role"] == "user"
        assert out[0]["content"]  # non-empty placeholder

    def test_nonempty_preserved_and_count_stable(self):
        h = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]
        out = _normalize_history_for_api(h)
        assert len(out) == 2
        assert out[0]["content"] == "hi"
        assert out[1]["content"] == [{"type": "text", "text": "ok"}]


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
    async def test_thinking_only_pause_injects_continuation(self, watchdog):
        # Turn 0: stop_reason=tool_use but only a thinking block (zero
        # tool_use blocks) → pause turn. Must NOT append empty content; must
        # nudge with a continuation user message. Turn 1: normal end_turn.
        client = _make_client(
            ([], _final_msg([_block("thinking", thinking="hmm")], "tool_use")),
            ([], _final_msg([_block("text", text="done")], "end_turn")),
        )
        history: list[dict] = []
        result = await run_agent_loop(
            "hi", channel_id=1, history=history,
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-8", extra_betas=[],
        )
        assert result.text == "done"
        # No empty-content user message slipped into history (the 400 cause).
        assert all(m["content"] != [] and m["content"] != "" for m in history)
        # A continuation nudge was injected as a user message.
        assert any(
            m["role"] == "user"
            and isinstance(m["content"], list)
            and m["content"]
            and m["content"][0].get("text") == "続けてください。"
            for m in history
        )

    @pytest.mark.asyncio
    async def test_thinking_only_pause_limit_cuts_off(self, watchdog):
        # Two consecutive pause turns hit MAX_CONSECUTIVE_EMPTY_TURNS (=2)
        # and the loop returns accumulated text instead of spinning forever.
        assert MAX_CONSECUTIVE_EMPTY_TURNS == 2
        client = _make_client(
            ([], _final_msg([_block("text", text="partial"), _block("thinking", thinking="x")], "tool_use")),
            ([], _final_msg([_block("thinking", thinking="y")], "tool_use")),
        )
        history: list[dict] = []
        result = await run_agent_loop(
            "hi", channel_id=1, history=history,
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-8", extra_betas=[],
        )
        assert "partial" in result.text

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


class TestHallucinationDetect:
    def test_detects_jp_claim_without_write_fire(self, caplog):
        caplog.set_level("WARNING")
        _detect_write_hallucination(
            "ドキュメントを /tmp/spec.md に書きました。確認してください。",
            fired_tools=set(),
            channel_id=42,
            turn=0,
        )
        assert any(
            "hallucination_suspect" in r.message for r in caplog.records
        )

    def test_detects_en_claim_without_write_fire(self, caplog):
        caplog.set_level("WARNING")
        _detect_write_hallucination(
            "I wrote to docs/foo.md with the plan.",
            fired_tools={"Read"},
            channel_id=1,
            turn=0,
        )
        assert any(
            "hallucination_suspect" in r.message for r in caplog.records
        )

    def test_silent_when_write_fired(self, caplog):
        caplog.set_level("WARNING")
        _detect_write_hallucination(
            "ドキュメントを /tmp/spec.md に書きました。",
            fired_tools={"Write"},
            channel_id=1,
            turn=0,
        )
        assert not any(
            "hallucination_suspect" in r.message for r in caplog.records
        )

    def test_silent_when_edit_fired(self, caplog):
        caplog.set_level("WARNING")
        _detect_write_hallucination(
            "/tmp/spec.md に保存しました。",
            fired_tools={"Edit"},
            channel_id=1,
            turn=0,
        )
        assert not any(
            "hallucination_suspect" in r.message for r in caplog.records
        )

    def test_silent_when_no_path_in_text(self, caplog):
        # Verb-only matches (no plausible filename) must not fire — too noisy.
        caplog.set_level("WARNING")
        _detect_write_hallucination(
            "対応しました。次は何しますか？",
            fired_tools=set(),
            channel_id=1,
            turn=0,
        )
        assert not any(
            "hallucination_suspect" in r.message for r in caplog.records
        )

    def test_silent_when_text_empty(self, caplog):
        caplog.set_level("WARNING")
        _detect_write_hallucination(
            "",
            fired_tools=set(),
            channel_id=1,
            turn=0,
        )
        assert caplog.records == []


class TestTodoWriteDisplay:
    def test_format_invocation_renders_counts(self):
        out = _format_tool_invocation_for_display(
            "TodoWrite",
            {"todos": [
                {"content": "a", "status": "in_progress", "activeForm": "A"},
                {"content": "b", "status": "pending", "activeForm": "B"},
                {"content": "c", "status": "completed", "activeForm": "C"},
            ]},
        )
        assert out is not None
        assert "TodoWrite" in out
        assert "in_progress=1" in out
        assert "pending=1" in out
        assert "completed=1" in out
        assert "[~] a" in out
        assert "[ ] b" in out
        assert "[x] c" in out


class TestEffortMax:
    """Pin the effort dial at "max" (top of low<medium<high<xhigh<max).

    The CLI capture shows "high" because that's claude-cli's default, but
    the dial actually goes higher and the bot opts in deliberately. These
    tests fail loudly if anyone silently downgrades the bot's request.
    """

    def test_output_config_constant_is_max(self):
        from services.claude_api.client import OUTPUT_CONFIG
        assert OUTPUT_CONFIG == {"effort": "max"}

    def test_effort_beta_still_in_betas(self):
        # output_config.effort is only honored under the effort-2025-11-24
        # beta — silently dropping the beta would make "max" a no-op.
        from services.claude_api.client import MODEL_BETAS
        assert "effort-2025-11-24" in MODEL_BETAS

    @pytest.mark.asyncio
    async def test_effort_max_reaches_stream_kwargs(self, watchdog):
        # Capture the kwargs that run_agent_loop hands to the SDK's stream
        # factory. The extra_body branch is where output_config rides into
        # the /v1/messages request body.
        captured_kwargs: dict = {}

        def stream_factory(**kwargs):
            captured_kwargs.update(kwargs)
            return _FakeStream(
                [],
                _final_msg([_block("text", text="ok")], "end_turn"),
            )
        client = MagicMock()
        client.beta.messages.stream = stream_factory

        await run_agent_loop(
            "hi", channel_id=1, history=[],
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all,
            execute_tool=AsyncMock(),
            status_callback=_noop_status,
            max_turns=1,
            stall_watchdog=watchdog,
            api_client=client,
            model="claude-opus-4-7",
            extra_betas=[],
        )
        extra_body = captured_kwargs.get("extra_body") or {}
        assert extra_body.get("output_config") == {"effort": "max"}

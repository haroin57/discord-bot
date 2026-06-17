"""Tests for services.claude_api.tool_loop.run_agent_loop."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from services.claude_api.stall import StallWatchdog
from services.claude_api.tool_loop import (
    MAX_CONSECUTIVE_EMPTY_TURNS,
    REFUSAL_FALLBACK_HEADER,
    AgentLoopResult,
    _block_to_dict,
    _build_cached_system,
    _detect_write_hallucination,
    _format_tool_invocation_for_display,
    _inject_cache_breakpoints,
    _join_final_text,
    _log_turn_usage,
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


class TestEffortMedium:
    """Pin the effort dial at the CLAUDE_EFFORT env value."""

    def test_output_config_constant_is_medium(self):
        # 2026-06-17: the default was raised from "medium" to env-driven
        # (commit 09d8a31 set .env CLAUDE_EFFORT=max via xhigh). What matters is
        # that OUTPUT_CONFIG reflects the configured value, not a hard pin.
        from config import CLAUDE_EFFORT
        from services.claude_api.client import OUTPUT_CONFIG
        assert OUTPUT_CONFIG == {"effort": CLAUDE_EFFORT}

    def test_effort_beta_still_in_betas(self):
        # output_config.effort is only honored under the effort-2025-11-24
        # beta — silently dropping the beta would make the pin a no-op.
        from services.claude_api.client import MODEL_BETAS
        assert "effort-2025-11-24" in MODEL_BETAS

    @pytest.mark.asyncio
    async def test_effort_medium_reaches_stream_kwargs(self, watchdog):
        # Capture the kwargs that run_agent_loop hands to the SDK's stream
        # factory. Per ADR-0009, output_config is a DIRECT kwarg (not nested in
        # extra_body) — the beta streaming path silently drops it from
        # extra_body, so effort never reaches the wire there. extra_body still
        # carries context_management.
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
        from config import CLAUDE_EFFORT
        assert captured_kwargs.get("output_config") == {"effort": CLAUDE_EFFORT}


class TestRefusalLogging:
    """ADR-0008: on stop_reason=refusal, _log_turn_usage must surface
    stop_details (category + explanation) so refusals are diagnosable."""

    def _usage(self):
        return SimpleNamespace(
            input_tokens=10, cache_read_input_tokens=0,
            cache_creation_input_tokens=100, output_tokens=2,
        )

    def test_refusal_logs_stop_details(self, caplog):
        caplog.set_level("WARNING")
        final = SimpleNamespace(
            usage=self._usage(),
            stop_reason="refusal",
            stop_details=SimpleNamespace(category="cyber", explanation="nope"),
        )
        _log_turn_usage(123, 0, final, {f: 0 for f in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens", "turns")})
        rec = [r.message for r in caplog.records if "claude_api_refusal" in r.message]
        assert rec, "refusal log not emitted"
        assert "category=cyber" in rec[0]
        assert "nope" in rec[0]

    def test_non_refusal_does_not_log_refusal(self, caplog):
        caplog.set_level("WARNING")
        final = SimpleNamespace(
            usage=self._usage(), stop_reason="end_turn", stop_details=None,
        )
        _log_turn_usage(123, 0, final, {f: 0 for f in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens", "turns")})
        assert not any("claude_api_refusal" in r.message for r in caplog.records)

    def test_refusal_with_missing_stop_details_is_safe(self, caplog):
        # Older SDK / mock without stop_details — must not crash, logs None.
        caplog.set_level("WARNING")
        final = SimpleNamespace(usage=self._usage(), stop_reason="refusal")
        _log_turn_usage(123, 0, final, {f: 0 for f in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens", "turns")})
        rec = [r.message for r in caplog.records if "claude_api_refusal" in r.message]
        assert rec and "category=None" in rec[0]


class TestRefusalFallback:
    """Fable 5 refusal -> Opus 4.8 fallback, surfaced (not silent)."""

    @staticmethod
    def _client(*responses):
        models = []
        it = iter(responses)
        client = MagicMock()

        def factory(**kwargs):
            models.append(kwargs.get("model"))
            events, final = next(it)
            return _FakeStream(events, final)

        client.beta.messages.stream = factory
        return client, models

    @pytest.mark.asyncio
    async def test_fallback_on_refusal_switches_model_and_notes(self, watchdog):
        client, models = self._client(
            ([], _final_msg([], "refusal")),                       # Fable refuses
            ([], _final_msg([_block("text", text="opus answer")], "end_turn")),
        )
        result = await run_agent_loop(
            "hi", channel_id=1, history=[], system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5, stall_watchdog=watchdog,
            api_client=client, model="claude-fable-5", extra_betas=[],
            refusal_fallback_model="claude-opus-4-8",
        )
        assert result.fallback_model == "claude-opus-4-8"
        assert "opus answer" in result.text
        # non-streaming -> note prepended into the returned text
        assert "Fable 5 が応答を拒否" in result.text
        # the retry used the fallback model
        assert models == ["claude-fable-5", "claude-opus-4-8"]

    @pytest.mark.asyncio
    async def test_no_fallback_when_model_unset(self, watchdog):
        client, models = self._client(([], _final_msg([], "refusal")))
        result = await run_agent_loop(
            "hi", channel_id=1, history=[], system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5, stall_watchdog=watchdog,
            api_client=client, model="claude-fable-5", extra_betas=[],
            refusal_fallback_model=None,
        )
        assert result.fallback_model is None
        assert result.text == "(no response)"
        assert models == ["claude-fable-5"]  # no retry

    @pytest.mark.asyncio
    async def test_fallback_note_sent_live_when_streaming(self, watchdog):
        sent = []

        async def inc(t):
            sent.append(t)

        client, _models = self._client(
            ([], _final_msg([], "refusal")),
            ([], _final_msg([_block("text", text="opus answer")], "end_turn")),
        )
        result = await run_agent_loop(
            "hi", channel_id=1, history=[], system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, incremental_text_callback=inc,
            max_turns=5, stall_watchdog=watchdog, api_client=client,
            model="claude-fable-5", extra_betas=[],
            refusal_fallback_model="claude-opus-4-8",
        )
        # note delivered live; NOT duplicated into the returned text
        assert any("Fable 5 が応答を拒否" in s for s in sent)
        assert "opus answer" in result.text
        assert "Fable 5 が応答を拒否" not in result.text
        assert result.fallback_model == "claude-opus-4-8"

    @pytest.mark.asyncio
    async def test_no_refallback_when_opus_also_refuses(self, watchdog):
        client, models = self._client(
            ([], _final_msg([], "refusal")),   # Fable refuses
            ([], _final_msg([], "refusal")),   # Opus also refuses
        )
        result = await run_agent_loop(
            "hi", channel_id=1, history=[], system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5, stall_watchdog=watchdog,
            api_client=client, model="claude-fable-5", extra_betas=[],
            refusal_fallback_model="claude-opus-4-8",
        )
        # one fallback only — no infinite loop
        assert models == ["claude-fable-5", "claude-opus-4-8"]
        assert result.text == "(no response)"
        assert result.fallback_model == "claude-opus-4-8"


class TestTextInvokeCorrection:
    """A tool call emitted as plain text triggers one corrective re-prompt,
    and the raw XML never reaches the returned text (2026-06-13 poison)."""

    _POISON = (
        '初値、確認します。\n\ncourse\n<invoke name="brave_news_search">\n'
        '<parameter name="query">x</parameter>\n</invoke>'
    )

    @staticmethod
    def _counting_client(*responses):
        calls = []
        it = iter(responses)
        client = MagicMock()

        def factory(**kwargs):
            calls.append(kwargs)
            events, final = next(it)
            return _FakeStream(events, final)

        client.beta.messages.stream = factory
        return client, calls

    @pytest.mark.asyncio
    async def test_corrective_retry_then_clean_answer(self, watchdog):
        client, calls = self._counting_client(
            ([], _final_msg([_block("text", text=self._POISON)], "end_turn")),
            ([], _final_msg([_block("text", text="正しい答えです")], "end_turn")),
        )
        result = await run_agent_loop(
            "調べて", channel_id=1, history=[], system_prompt="sys",
            tools_for_api=[], can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5, stall_watchdog=watchdog,
            api_client=client, model="claude-opus-4-8", extra_betas=[],
        )
        assert len(calls) == 2, "should re-prompt once after a text-form call"
        assert "正しい答え" in result.text
        assert "<invoke" not in result.text

    @pytest.mark.asyncio
    async def test_exhausted_correction_strips_raw_xml(self, watchdog):
        poison2 = (
            'やはりこちら。\n<invoke name="Bash">\n'
            '<parameter name="command">ls</parameter>\n</invoke>'
        )
        client, calls = self._counting_client(
            ([], _final_msg([_block("text", text=self._POISON)], "end_turn")),
            ([], _final_msg([_block("text", text=poison2)], "end_turn")),
        )
        result = await run_agent_loop(
            "調べて", channel_id=1, history=[], system_prompt="sys",
            tools_for_api=[], can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5, stall_watchdog=watchdog,
            api_client=client, model="claude-opus-4-8", extra_betas=[],
        )
        # one correction only, then give up — no infinite loop
        assert len(calls) == 2
        # the raw XML is stripped from the returned text
        assert "<invoke" not in result.text
        assert "やはりこちら。" in result.text

    @pytest.mark.asyncio
    async def test_real_tool_use_with_invoke_text_not_flagged(self, watchdog):
        # If the turn has a REAL tool_use block, an <invoke>-looking string in
        # accompanying prose must NOT trigger the correction (the call ran).
        client, calls = self._counting_client(
            ([], _final_msg([
                _block("text", text="動かします"),
                _block("tool_use", id="t1", name="Read", input={"path": "/x"}),
            ], "tool_use")),
            ([], _final_msg([_block("text", text="完了")], "end_turn")),
        )
        result = await run_agent_loop(
            "読んで", channel_id=1, history=[], system_prompt="sys",
            tools_for_api=[{"name": "Read"}], can_use_tool=_allow_all,
            execute_tool=AsyncMock(return_value="ok"),
            status_callback=_noop_status, max_turns=5, stall_watchdog=watchdog,
            api_client=client, model="claude-opus-4-8", extra_betas=[],
        )
        assert "完了" in result.text


class TestRefusalFallbackHeader:
    """x-is-refusal-fallback: true on fallback-leg requests (ADR-0010).

    Mirrors the official CLI, whose fetch wrapper sets the header on every
    request once a refusal fallback has occurred in the session."""

    @staticmethod
    def _client(*responses):
        headers = []
        it = iter(responses)
        client = MagicMock()

        def factory(**kwargs):
            headers.append(kwargs.get("extra_headers"))
            events, final = next(it)
            return _FakeStream(events, final)

        client.beta.messages.stream = factory
        return client, headers

    @pytest.mark.asyncio
    async def test_retry_leg_sets_header_first_request_does_not(self, watchdog):
        client, headers = self._client(
            ([], _final_msg([], "refusal")),
            ([], _final_msg([_block("text", text="opus answer")], "end_turn")),
        )
        await run_agent_loop(
            "hi", channel_id=1, history=[], system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5, stall_watchdog=watchdog,
            api_client=client, model="claude-fable-5", extra_betas=[],
            refusal_fallback_model="claude-opus-4-8",
        )
        assert headers[0] is None  # primary leg: no header
        assert headers[1] == {REFUSAL_FALLBACK_HEADER: "true"}  # retry leg

    @pytest.mark.asyncio
    async def test_sticky_session_extra_headers_pass_through(self, watchdog):
        # entry.py sends the header via extra_headers on sticky-pinned turns;
        # it must reach the request even when no in-loop fallback happens.
        client, headers = self._client(
            ([], _final_msg([_block("text", text="ok")], "end_turn")),
        )
        await run_agent_loop(
            "hi", channel_id=1, history=[], system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=1, stall_watchdog=watchdog,
            api_client=client, model="claude-opus-4-8", extra_betas=[],
            extra_headers={REFUSAL_FALLBACK_HEADER: "true"},
        )
        assert headers[0] == {REFUSAL_FALLBACK_HEADER: "true"}

    @pytest.mark.asyncio
    async def test_fallback_header_merges_with_existing_extra_headers(self, watchdog):
        client, headers = self._client(
            ([], _final_msg([], "refusal")),
            ([], _final_msg([_block("text", text="opus answer")], "end_turn")),
        )
        await run_agent_loop(
            "hi", channel_id=1, history=[], system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5, stall_watchdog=watchdog,
            api_client=client, model="claude-fable-5", extra_betas=[],
            refusal_fallback_model="claude-opus-4-8",
            extra_headers={"x-custom": "keep-me"},
        )
        assert headers[0] == {"x-custom": "keep-me"}
        assert headers[1] == {
            "x-custom": "keep-me",
            REFUSAL_FALLBACK_HEADER: "true",
        }


class TestCacheBreakpointTTL:
    """The live tool-loop path must put ttl:1h on every cache breakpoint.

    Regression guard for the bug where the message-level breakpoint used the
    default 5-minute TTL while system[2]/[3] used 1h. Discord turns are spaced
    >5 min apart routinely, so a 5-min breakpoint expired between user turns and
    the whole replayed history was re-written to cache on every resume. All
    breakpoints must be 1h so the history prefix survives the inter-turn gap.
    """

    _FIVE_MSGS = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "r1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "r2"},
        {"role": "user", "content": "third"},
    ]

    def test_string_content_breakpoint_is_1h(self):
        result = _inject_cache_breakpoints(self._FIVE_MSGS)
        # second-to-last user (index 2, "second") becomes the breakpoint
        block = result[2]["content"][0]
        assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_list_content_breakpoint_is_1h(self):
        msgs = list(self._FIVE_MSGS)
        msgs[2] = {"role": "user", "content": [
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        result = _inject_cache_breakpoints(msgs)
        assert result[2]["content"][-1]["cache_control"] == {
            "type": "ephemeral", "ttl": "1h"}

    def test_system_and_message_breakpoints_share_1h_ttl(self):
        # All cache breakpoints across the request must agree on ttl:1h so the
        # cached prefix expires as one unit, not piecemeal.
        system = _build_cached_system("persona", self._FIVE_MSGS)
        sys_ttls = [b["cache_control"]["ttl"] for b in system
                    if "cache_control" in b]
        assert sys_ttls == ["1h", "1h"]
        msg_block = _inject_cache_breakpoints(self._FIVE_MSGS)[2]["content"][0]
        assert msg_block["cache_control"]["ttl"] == "1h"

    def test_breakpoint_count_within_limit(self):
        # Anthropic allows max 4 cache_control breakpoints per request. We use
        # system[2] + system[3] + one message breakpoint = 3. Guard the budget.
        system = _build_cached_system("persona", self._FIVE_MSGS)
        msgs = _inject_cache_breakpoints(self._FIVE_MSGS)
        n = sum(1 for b in system if "cache_control" in b)
        for m in msgs:
            c = m["content"]
            if isinstance(c, list):
                n += sum(1 for b in c if isinstance(b, dict) and "cache_control" in b)
        assert n <= 4

    def test_render_is_deterministic(self):
        # The behavioural claim behind ttl:1h is that the NEXT turn can READ the
        # cache this turn wrote. That only holds if re-rendering the same inputs
        # produces byte-identical system + breakpoint bytes — a per-request
        # timestamp/uuid/non-deterministic JSON anywhere upstream of the
        # breakpoint would silently invalidate the prefix every turn (the exact
        # symptom this fix targets). Lock determinism so a future silent
        # invalidator fails loudly here instead of in production cache_read=0.
        import json
        sys_a = json.dumps(_build_cached_system("persona", self._FIVE_MSGS))
        sys_b = json.dumps(_build_cached_system("persona", self._FIVE_MSGS))
        msg_a = json.dumps(_inject_cache_breakpoints(self._FIVE_MSGS))
        msg_b = json.dumps(_inject_cache_breakpoints(self._FIVE_MSGS))
        assert sys_a == sys_b, "system render is non-deterministic — cache prefix will churn"
        assert msg_a == msg_b, "message render is non-deterministic — cache prefix will churn"

    def test_breakpoint_message_text_is_preserved(self):
        # Converting a string-content message to a [{text}] block to carry the
        # breakpoint must not alter the text itself — only wrap it. (The server
        # normalizes "x" and [{type:text,text:"x"}] to the same content hash, so
        # the wrap is cache-neutral; what matters is the text survives intact.)
        result = _inject_cache_breakpoints(self._FIVE_MSGS)
        assert result[2]["content"][0]["text"] == "second"
        # And the original history object is not mutated by the wrap.
        assert self._FIVE_MSGS[2]["content"] == "second"


class TestDualCacheBreakpoints:
    """Audit #8 (binary fn Def): breakpoints on the LAST and SECOND-TO-LAST
    user turns, so the next request reads this turn's exchange from cache."""

    _MSGS = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "r1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "r2"},
        {"role": "user", "content": "third"},
    ]

    def _has_bp(self, msg):
        c = msg["content"]
        return isinstance(c, list) and any(
            isinstance(b, dict) and "cache_control" in b for b in c
        )

    def test_last_and_second_to_last_user_marked(self):
        out = _inject_cache_breakpoints(self._MSGS)
        assert self._has_bp(out[2])
        assert self._has_bp(out[4])
        assert not self._has_bp(out[0])

    def test_first_turn_single_user_marked(self):
        out = _inject_cache_breakpoints([{"role": "user", "content": "hi"}])
        assert self._has_bp(out[0])

    def test_no_user_messages_noop(self):
        msgs = [{"role": "assistant", "content": "r"}]
        assert _inject_cache_breakpoints(msgs) == msgs

    def test_total_breakpoints_at_api_limit(self):
        # 2 system + 2 message breakpoints = the API maximum of 4.
        system = _build_cached_system("persona", self._MSGS)
        msgs = _inject_cache_breakpoints(self._MSGS)
        n = sum(1 for b in system if "cache_control" in b)
        for m in msgs:
            c = m["content"]
            if isinstance(c, list):
                n += sum(1 for b in c
                         if isinstance(b, dict) and "cache_control" in b)
        assert n == 4


class TestToolInputValidation:
    """Audit #11: malformed tool input → <tool_use_error> result, no dispatch."""

    _SCHEMA = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    }

    @pytest.mark.asyncio
    async def test_invalid_input_returns_tool_use_error(self, watchdog):
        client = _make_client(
            ([], _final_msg(
                [_block("tool_use", id="t1", name="Read", input={})],
                "tool_use",
            )),
            ([], _final_msg([_block("text", text="done")], "end_turn")),
        )
        execute = AsyncMock(return_value="never")
        history: list[dict] = []
        result = await run_agent_loop(
            "read", channel_id=1, history=history, system_prompt="s",
            tools_for_api=[{"name": "Read", "description": "",
                            "input_schema": self._SCHEMA}],
            can_use_tool=_allow_all, execute_tool=execute,
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-6", extra_betas=[],
        )
        execute.assert_not_awaited()
        tr = history[2]["content"][0]
        assert tr["is_error"] is True
        assert tr["content"].startswith("<tool_use_error>InputValidationError")
        assert "file_path" in tr["content"]
        assert result.text == "done"

    @pytest.mark.asyncio
    async def test_valid_input_dispatches(self, watchdog):
        client = _make_client(
            ([], _final_msg(
                [_block("tool_use", id="t1", name="Read",
                        input={"file_path": "/x"})],
                "tool_use",
            )),
            ([], _final_msg([_block("text", text="done")], "end_turn")),
        )
        execute = AsyncMock(return_value="data")
        history: list[dict] = []
        await run_agent_loop(
            "read", channel_id=1, history=history, system_prompt="s",
            tools_for_api=[{"name": "Read", "description": "",
                            "input_schema": self._SCHEMA}],
            can_use_tool=_allow_all, execute_tool=execute,
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-6", extra_betas=[],
        )
        execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handler_error_uses_tool_use_error_format(self, watchdog):
        client = _make_client(
            ([], _final_msg(
                [_block("tool_use", id="t1", name="Read",
                        input={"file_path": "/x"})],
                "tool_use",
            )),
            ([], _final_msg([_block("text", text="done")], "end_turn")),
        )
        execute = AsyncMock(side_effect=ValueError("boom"))
        history: list[dict] = []
        await run_agent_loop(
            "read", channel_id=1, history=history, system_prompt="s",
            tools_for_api=[{"name": "Read", "description": "",
                            "input_schema": self._SCHEMA}],
            can_use_tool=_allow_all, execute_tool=execute,
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-6", extra_betas=[],
        )
        tr = history[2]["content"][0]
        assert tr["is_error"] is True
        assert tr["content"] == "<tool_use_error>boom</tool_use_error>"


class TestParallelToolExecution:
    """Audit #12: read-only batches run concurrently, order preserved."""

    def _read_blocks(self, n):
        return [
            _block("tool_use", id=f"t{i}", name="Read",
                   input={"file_path": f"/f{i}"})
            for i in range(n)
        ]

    def _tracking_executor(self):
        import asyncio as _asyncio
        state = {"active": 0, "peak": 0}

        async def execute(name, args):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await _asyncio.sleep(0.02)
            state["active"] -= 1
            return "data"

        return execute, state

    @pytest.mark.asyncio
    async def test_read_batch_runs_concurrently_order_preserved(self, watchdog):
        client = _make_client(
            ([], _final_msg(self._read_blocks(3), "tool_use")),
            ([], _final_msg([_block("text", text="done")], "end_turn")),
        )
        execute, state = self._tracking_executor()
        history: list[dict] = []
        await run_agent_loop(
            "go", channel_id=1, history=history, system_prompt="s",
            tools_for_api=[{"name": "Read", "description": "",
                            "input_schema": {}}],
            can_use_tool=_allow_all, execute_tool=execute,
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-6", extra_betas=[],
        )
        assert state["peak"] >= 2
        ids = [b["tool_use_id"] for b in history[2]["content"]]
        assert ids == ["t0", "t1", "t2"]

    @pytest.mark.asyncio
    async def test_unsafe_batch_stays_sequential(self, watchdog):
        blocks = self._read_blocks(2) + [
            _block("tool_use", id="t9", name="Bash",
                   input={"command": "ls"}),
        ]
        client = _make_client(
            ([], _final_msg(blocks, "tool_use")),
            ([], _final_msg([_block("text", text="done")], "end_turn")),
        )
        execute, state = self._tracking_executor()
        history: list[dict] = []
        await run_agent_loop(
            "go", channel_id=1, history=history, system_prompt="s",
            tools_for_api=[], can_use_tool=_allow_all, execute_tool=execute,
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-6", extra_betas=[],
        )
        assert state["peak"] == 1


class TestUsageObservability:
    """Audit #17: context-token tracking feeds the token compaction trigger."""

    def test_get_last_context_tokens_updated(self):
        from services.claude_api.tool_loop import get_last_context_tokens
        totals = {f: 0 for f in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens")}
        totals["turns"] = 0
        msg = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=10, cache_read_input_tokens=20,
                cache_creation_input_tokens=30, output_tokens=1,
            ),
            stop_reason="end_turn",
        )
        _log_turn_usage(424242, 0, msg, totals)
        assert get_last_context_tokens(424242) == 60

    def test_no_usage_keeps_silent(self):
        from services.claude_api.tool_loop import get_last_context_tokens
        totals = {"turns": 0}
        _log_turn_usage(424243, 0, SimpleNamespace(usage=None), totals)
        assert get_last_context_tokens(424243) == 0


class TestInLoopProactiveCompaction:
    """Audit #3 / ADR-0012: the loop folds the prior window mid-request when
    the last turn's context footprint crosses the proactive floor."""

    def _usage(self, total):
        return SimpleNamespace(
            input_tokens=total, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, output_tokens=5,
        )

    def _turns(self, usage):
        final1 = SimpleNamespace(
            content=[_block("tool_use", id="t1", name="Read",
                            input={"file_path": "/x"})],
            stop_reason="tool_use", usage=usage,
        )
        final2 = SimpleNamespace(
            content=[_block("text", text="done")],
            stop_reason="end_turn", usage=usage,
        )
        return ([], final1), ([], final2)

    @pytest.mark.asyncio
    async def test_compacts_and_rebases_prior_window(self, watchdog, monkeypatch):
        import services.claude_api.tool_loop as tl
        monkeypatch.setattr(tl, "PROACTIVE_COMPACT_ENABLED", True)
        monkeypatch.setattr(tl, "PROACTIVE_COMPACT_TRIGGER_TOKENS", 100)
        client = _make_client(*self._turns(self._usage(200)))
        prior = [
            {"role": "user", "content": "old q"},
            {"role": "assistant", "content": "old a"},
        ]
        history = list(prior)
        new_prior = [{"role": "user", "content": "COMPACT SUMMARY"}]
        compact_cb = AsyncMock(return_value=new_prior)
        result = await run_agent_loop(
            "hi", channel_id=1, history=history, system_prompt="s",
            tools_for_api=[], can_use_tool=_allow_all,
            execute_tool=AsyncMock(return_value="data"),
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-6", extra_betas=[],
            proactive_compact=compact_cb, prior_turns=len(prior),
        )
        compact_cb.assert_awaited_once()
        assert history[0] == {"role": "user", "content": "COMPACT SUMMARY"}
        assert result.prior_len == 1
        # The current request's expansion survives after the new prior window.
        assert history[1] == {"role": "user", "content": "hi"}

    @pytest.mark.asyncio
    async def test_below_floor_never_compacts(self, watchdog, monkeypatch):
        import services.claude_api.tool_loop as tl
        monkeypatch.setattr(tl, "PROACTIVE_COMPACT_ENABLED", True)
        monkeypatch.setattr(tl, "PROACTIVE_COMPACT_TRIGGER_TOKENS", 10**9)
        client = _make_client(*self._turns(self._usage(200)))
        compact_cb = AsyncMock()
        prior = [{"role": "user", "content": "old"}]
        history = list(prior)
        result = await run_agent_loop(
            "hi", channel_id=1, history=history, system_prompt="s",
            tools_for_api=[], can_use_tool=_allow_all,
            execute_tool=AsyncMock(return_value="data"),
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-6", extra_betas=[],
            proactive_compact=compact_cb, prior_turns=len(prior),
        )
        compact_cb.assert_not_awaited()
        assert result.prior_len == len(prior)

    @pytest.mark.asyncio
    async def test_callback_failure_is_nonfatal(self, watchdog, monkeypatch):
        import services.claude_api.tool_loop as tl
        monkeypatch.setattr(tl, "PROACTIVE_COMPACT_ENABLED", True)
        monkeypatch.setattr(tl, "PROACTIVE_COMPACT_TRIGGER_TOKENS", 100)
        client = _make_client(*self._turns(self._usage(200)))
        compact_cb = AsyncMock(side_effect=RuntimeError("summary failed"))
        prior = [{"role": "user", "content": "old"}]
        history = list(prior)
        result = await run_agent_loop(
            "hi", channel_id=1, history=history, system_prompt="s",
            tools_for_api=[], can_use_tool=_allow_all,
            execute_tool=AsyncMock(return_value="data"),
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-6", extra_betas=[],
            proactive_compact=compact_cb, prior_turns=len(prior),
        )
        assert result.text == "done"
        assert result.prior_len == len(prior)


class TestUsageLogModel:
    def test_model_appears_in_usage_log(self, caplog):
        import logging
        from services.claude_api.tool_loop import _log_turn_usage
        totals = {f: 0 for f in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens")}
        totals["turns"] = 0
        msg = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=1, cache_read_input_tokens=2,
                cache_creation_input_tokens=3, output_tokens=4,
            ),
            stop_reason="end_turn",
        )
        with caplog.at_level(logging.INFO, logger="services.claude_api.tool_loop"):
            _log_turn_usage(424244, 0, msg, totals, model="claude-fable-5")
        assert any("model=claude-fable-5" in r.message for r in caplog.records)

    def test_missing_model_logs_dash(self, caplog):
        import logging
        from services.claude_api.tool_loop import _log_turn_usage
        totals = {f: 0 for f in (
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens")}
        totals["turns"] = 0
        msg = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=1, cache_read_input_tokens=2,
                cache_creation_input_tokens=3, output_tokens=4,
            ),
            stop_reason="end_turn",
        )
        with caplog.at_level(logging.INFO, logger="services.claude_api.tool_loop"):
            _log_turn_usage(424245, 0, msg, totals)
        assert any("model=-" in r.message for r in caplog.records)


class TestServerSideFallback:
    """ADR-0015: send the `fallbacks` body field and detect server-side
    mid-stream model switches."""

    def _capture_client(self, events, final):
        captured: dict = {}

        def stream_factory(**kwargs):
            captured.update(kwargs)
            return _FakeStream(events, final)
        client = MagicMock()
        client.beta.messages.stream = stream_factory
        return client, captured

    async def _run(self, client, **over):
        kw = dict(
            prompt="hi", channel_id=1, history=[], system_prompt="s",
            tools_for_api=[], can_use_tool=_allow_all,
            execute_tool=AsyncMock(), status_callback=_noop_status,
            max_turns=1, stall_watchdog=over.pop("watchdog"),
            api_client=client, model="claude-fable-5", extra_betas=[],
            refusal_fallback_model="claude-opus-4-8",
        )
        kw.update(over)
        return await run_agent_loop(**kw)

    @pytest.mark.asyncio
    async def test_fallbacks_field_sent_when_refusal_model_set(self, watchdog):
        client, cap = self._capture_client(
            [], _final_msg([_block("text", text="ok")], "end_turn"))
        await self._run(client, watchdog=watchdog)
        assert cap["extra_body"]["fallbacks"] == [{"model": "claude-opus-4-8"}]

    @pytest.mark.asyncio
    async def test_fallbacks_not_sent_without_refusal_model(self, watchdog):
        client, cap = self._capture_client(
            [], _final_msg([_block("text", text="ok")], "end_turn"))
        await self._run(client, watchdog=watchdog,
                        model="claude-opus-4-7", refusal_fallback_model=None)
        assert "fallbacks" not in cap["extra_body"]

    @pytest.mark.asyncio
    async def test_fallbacks_gated_off_by_flag(self, watchdog, monkeypatch):
        import services.claude_api.tool_loop as tl
        monkeypatch.setattr(tl, "SERVER_SIDE_FALLBACK_ENABLED", False)
        client, cap = self._capture_client(
            [], _final_msg([_block("text", text="ok")], "end_turn"))
        await self._run(client, watchdog=watchdog)
        assert "fallbacks" not in cap["extra_body"]

    @pytest.mark.asyncio
    async def test_detect_via_fallback_content_block(self, watchdog):
        # Mid-stream content_block_start with type=fallback, to.model=opus.
        fb_event = _block(
            "content_block_start",
            content_block=_block("fallback",
                                 to=SimpleNamespace(model="claude-opus-4-8")),
        )
        client, _ = self._capture_client(
            [fb_event],
            _final_msg([_block("text", text="answer")], "end_turn"))
        sent: list[str] = []
        async def inc(t): sent.append(t)
        result = await self._run(client, watchdog=watchdog,
                                 incremental_text_callback=inc)
        assert result.fallback_model == "claude-opus-4-8"
        assert any("フォールバック" in s for s in sent)

    @pytest.mark.asyncio
    async def test_detect_via_final_model_divergence(self, watchdog):
        final = SimpleNamespace(
            content=[_block("text", text="answer")],
            stop_reason="end_turn", model="claude-opus-4-8",
            usage=None,
        )
        client, _ = self._capture_client([], final)
        result = await self._run(client, watchdog=watchdog)
        assert result.fallback_model == "claude-opus-4-8"

    @pytest.mark.asyncio
    async def test_no_false_positive_when_model_matches(self, watchdog):
        final = SimpleNamespace(
            content=[_block("text", text="answer")],
            stop_reason="end_turn", model="claude-fable-5", usage=None,
        )
        client, _ = self._capture_client([], final)
        result = await self._run(client, watchdog=watchdog)
        assert result.fallback_model is None

    @pytest.mark.asyncio
    async def test_client_side_backstop_still_fires_on_refusal(self, watchdog):
        # No server-side fallback signal; stop_reason=refusal must still trigger
        # the client-side retry on the fallback model.
        client = _make_client(
            ([], _final_msg([_block("text", text="")], "refusal")),
            ([], _final_msg([_block("text", text="recovered")], "end_turn")),
        )
        result = await self._run(client, watchdog=watchdog, max_turns=2)
        assert result.fallback_model == "claude-opus-4-8"
        assert "recovered" in result.text

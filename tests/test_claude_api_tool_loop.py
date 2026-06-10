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


class TestEffortXhigh:
    """Pin the effort dial at "xhigh" (low<medium<high<xhigh<max).

    The CLI capture shows "high" because that's claude-cli's default, but
    the bot deliberately steps up one notch to "xhigh" for extra thinking
    budget (an intentional divergence from the CLI request — haroin
    preference). These tests fail loudly if anyone silently changes it.
    """

    def test_output_config_constant_is_xhigh(self):
        from services.claude_api.client import OUTPUT_CONFIG
        assert OUTPUT_CONFIG == {"effort": "xhigh"}

    def test_effort_beta_still_in_betas(self):
        # output_config.effort is only honored under the effort-2025-11-24
        # beta — silently dropping the beta would make "max" a no-op.
        from services.claude_api.client import MODEL_BETAS
        assert "effort-2025-11-24" in MODEL_BETAS

    @pytest.mark.asyncio
    async def test_effort_xhigh_reaches_stream_kwargs(self, watchdog):
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
        assert captured_kwargs.get("output_config") == {"effort": "xhigh"}


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

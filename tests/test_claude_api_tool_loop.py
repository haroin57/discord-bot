"""Tests for services.claude_api.tool_loop.run_agent_loop."""

import asyncio
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
    _normalize_history_for_api,
    _parse_text_tool_calls,
    _strip_text_tool_calls,
    _truncate_tool_result,
    run_agent_loop,
)


def _block(type_, **kwargs):
    return SimpleNamespace(type=type_, **kwargs)


class TestCacheBreakpoints:
    """1h cache TTL so the prior_history prefix survives the 30-min session
    gap and is cache-read on resume (the dominant turn=1 write)."""

    def test_system_block_has_1h_ttl(self):
        sysblocks = _build_cached_system("persona", [{"role": "user", "content": "hi"}])
        cc = sysblocks[2].get("cache_control")
        assert cc is not None
        assert cc.get("ttl") == "1h"

    def test_message_breakpoint_has_1h_ttl(self):
        msgs = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3 current"},
        ]
        out = _inject_cache_breakpoints(msgs)
        # breakpoint goes on the second-to-last user message (u2, index 2).
        bp = out[2]["content"]
        block = bp[-1] if isinstance(bp, list) else None
        assert block is not None and block.get("cache_control", {}).get("ttl") == "1h"

    def test_ttl_ordering_invariant(self):
        # Anthropic requires longer-TTL cache_control BEFORE shorter ones
        # (tools→system→messages). We only ever emit 1h, so no 5m may precede.
        msgs = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
        ]
        sysblocks = _build_cached_system("persona", msgs)
        out = _inject_cache_breakpoints(msgs)
        ttls = []
        for b in sysblocks:
            cc = b.get("cache_control")
            if cc:
                ttls.append(cc.get("ttl", "5m"))
        for m in out:
            c = m["content"]
            if isinstance(c, list):
                for blk in c:
                    cc = isinstance(blk, dict) and blk.get("cache_control")
                    if cc:
                        ttls.append(cc.get("ttl", "5m"))
        # No 5-minute breakpoint may appear before any 1-hour one.
        assert all(t == "1h" for t in ttls), ttls


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


class TestStripTextToolCalls:
    """Tool calls the model wrote as TEXT (function-calling XML) must be
    stripped before they reach the user (msg3 root cause, rid=decc2aeb)."""

    def test_bare_invoke_block_stripped(self):
        text = (
            "裏取りします。\n"
            '<invoke name="mcp__brave-search__brave_web_search">\n'
            '<parameter name="query">Andrew Left</parameter>\n</invoke>'
        )
        cleaned, had = _strip_text_tool_calls(text)
        assert had is True
        assert "<invoke" not in cleaned and "parameter" not in cleaned
        assert cleaned == "裏取りします。"

    def test_function_calls_wrapper_stripped(self):
        text = (
            "ok\n<function_calls>\n<invoke name=\"x\">\n"
            "<parameter name=\"q\">v</parameter>\n</invoke>\n</function_calls>"
        )
        cleaned, had = _strip_text_tool_calls(text)
        assert had is True
        assert "function_calls" not in cleaned and cleaned.strip() == "ok"

    def test_namespaced_prefix_stripped(self):
        # Build the "antml:"-style namespace without writing the literal token.
        ns = "a" + "ntml:"
        text = (
            "keep\n"
            f'<{ns}function_calls><{ns}invoke name="t">'
            f"</{ns}invoke></{ns}function_calls>"
        )
        cleaned, had = _strip_text_tool_calls(text)
        assert had is True
        assert "function_calls" not in cleaned and cleaned.strip() == "keep"

    def test_no_markers_unchanged(self):
        text = "ただの文章です。a < b の比較や <url> も触らない。"
        cleaned, had = _strip_text_tool_calls(text)
        assert had is False
        assert cleaned == text

    def test_unclosed_marker_stripped(self):
        # Stream truncated mid-tag: an unclosed <invoke …> still gets removed.
        text = 'やります。\n<invoke name="Read">'
        cleaned, had = _strip_text_tool_calls(text)
        assert had is True
        assert "invoke" not in cleaned and cleaned.strip() == "やります。"


class TestParseTextToolCalls:
    """Text-format tool calls are parsed back into (name, input) so the loop
    can EXECUTE them instead of nudge-and-give-up (root cause 2)."""

    def test_single_call_string_param(self):
        text = (
            '<invoke name="mcp__brave-search__brave_web_search">'
            '<parameter name="query">Andrew Left</parameter></invoke>'
        )
        calls = _parse_text_tool_calls(text)
        assert calls == [("mcp__brave-search__brave_web_search", {"query": "Andrew Left"})]

    def test_multiple_calls(self):
        text = (
            '<invoke name="A"><parameter name="x">1one</parameter></invoke>'
            '<invoke name="B"><parameter name="y">two</parameter></invoke>'
        )
        calls = _parse_text_tool_calls(text)
        assert [c[0] for c in calls] == ["A", "B"]
        assert calls[1][1] == {"y": "two"}

    def test_json_typed_param_decoded(self):
        text = (
            '<invoke name="t"><parameter name="q">hi</parameter>'
            '<parameter name="count">12</parameter></invoke>'
        )
        _, inp = _parse_text_tool_calls(text)[0]
        assert inp["q"] == "hi"
        assert inp["count"] == 12  # JSON-decoded to int, not "12"

    def test_unclosed_invoke_not_parsed(self):
        # No closing </invoke> → cannot execute → caller falls back to a nudge.
        assert _parse_text_tool_calls('<invoke name="Read">') == []

    def test_no_markers_empty(self):
        assert _parse_text_tool_calls("ただの文章") == []


class TestTruncateToolResult:
    """Oversized tool results are head+tail capped to bound history growth
    (context_overflow prevention ①)."""

    def test_short_passthrough(self):
        assert _truncate_tool_result("small") == "small"

    def test_list_passthrough(self):
        blocks = [{"type": "text", "text": "x"}]
        assert _truncate_tool_result(blocks) is blocks

    def test_oversized_head_tail_truncated(self):
        import services.claude_api.tool_loop as tl
        cap = tl.MAX_TOOL_RESULT_CHARS
        big = ("H" * (cap + 20000)) + ("T" * 5000)
        out = _truncate_tool_result(big)
        assert len(out) < len(big)
        assert out.startswith("H")
        assert out.endswith("T")
        assert "elided" in out


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
    async def test_thinking_only_pause_terminates_without_empty_content(self, watchdog):
        # Under MAX_CONSECUTIVE_EMPTY_TURNS=2 the first thinking-only pause turn
        # nudges (one corrective POST); the SECOND consecutive pause hits the
        # limit and returns accumulated text. Critical invariants:
        #   1. No empty-content user message slipped into history (the 400 cause).
        #   2. The nudge IS appended, and it is a non-empty text block.
        client = _make_client(
            ([], _final_msg([_block("thinking", thinking="hmm")], "tool_use")),
            ([], _final_msg([_block("thinking", thinking="still")], "tool_use")),
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
        # No prior text turns → accumulated empty → (no response) fallback.
        assert result.text == "(no response)"
        # 400 guard: never append "" or [] content.
        assert all(m["content"] != [] and m["content"] != "" for m in history)
        # Exactly one nudge user message was appended (MAX=2 allows one nudge).
        nudges = [
            m for m in history
            if m["role"] == "user"
            and isinstance(m["content"], list)
            and m["content"]
            and isinstance(m["content"][0], dict)
            and "前ターンは思考のみで" in m["content"][0].get("text", "")
        ]
        assert len(nudges) == 1

    @pytest.mark.asyncio
    async def test_thinking_only_pause_nudges_then_returns_accumulated_text(self, watchdog):
        # Turn 0: prose "partial" + thinking + stop_reason=tool_use, no tool_use
        #         blocks → pause (consecutive=1) → nudge, a second POST fires.
        # Turn 1: pause again (consecutive=2 == MAX) → limit reached → return
        #         accumulated text ("partial"). This restores the nudge that
        #         MAX=1 had killed (msg3 premature-stop regression, 2026-06-02).
        assert MAX_CONSECUTIVE_EMPTY_TURNS == 2
        client = _make_client(
            ([], _final_msg(
                [_block("text", text="partial"), _block("thinking", thinking="x")],
                "tool_use",
            )),
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
        # The nudge was appended after the first pause turn.
        assert any(
            m["role"] == "user" and isinstance(m["content"], list)
            and m["content"] and isinstance(m["content"][0], dict)
            and "前ターンは思考のみで" in m["content"][0].get("text", "")
            for m in history
        )

    @pytest.mark.asyncio
    async def test_first_event_timeout_retries_then_returns(self, watchdog, monkeypatch):
        # An upstream hang = HTTP 200 + no SSE events. The first-event timeout
        # abandons the stream and re-POSTs once; if still silent it returns a
        # clean timeout instead of waiting out the full idle_timeout (msg1).
        import services.claude_api.tool_loop as tl
        monkeypatch.setattr(tl, "STREAM_FIRST_EVENT_TIMEOUT_S", 0.05)
        monkeypatch.setattr(tl, "STREAM_FIRST_EVENT_MAX_RETRIES", 1)

        class _HangStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(5)  # never delivers a first event
                raise StopAsyncIteration

            async def get_final_message(self):
                raise AssertionError("must not reach final message on a hang")

        attempts = {"n": 0}
        client = MagicMock()

        def stream_factory(**_k):
            attempts["n"] += 1
            return _HangStream()

        client.beta.messages.stream = stream_factory
        result = await run_agent_loop(
            "hi", channel_id=1, history=[],
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=AsyncMock(),
            status_callback=_noop_status, max_turns=5,
            stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-8", extra_betas=[],
        )
        # initial POST + 1 retry = 2, then a clean first-event-timeout message.
        assert attempts["n"] == 2
        assert "初動タイムアウト" in result.text

    @pytest.mark.asyncio
    async def test_text_format_tool_call_stripped_and_executed(self, watchdog):
        # Opus 4.8 sometimes writes a tool call as TEXT (function-calling XML)
        # instead of a native tool_use block (rid=decc2aeb msg3). The XML must
        # never reach the user, AND the call must be PARSED + EXECUTED (not just
        # nudged) so the model's intent isn't lost. Turn 0 leaks an <invoke>;
        # the loop runs it, feeds the result back; turn 1 ends cleanly.
        leak = (
            "了解です、裏取りします。\n"
            '<function_calls>\n<invoke name="mcp__brave-search__brave_web_search">\n'
            '<parameter name="query">Andrew Left verdict</parameter>\n'
            "</invoke>\n</function_calls>"
        )
        sent: list[str] = []

        async def _capture(text):
            sent.append(text)

        execute = AsyncMock(return_value="検索結果データ")
        client = _make_client(
            ([], _final_msg([_block("text", text=leak)], "tool_use")),
            ([], _final_msg([_block("text", text="done")], "end_turn")),
        )
        history: list[dict] = []
        result = await run_agent_loop(
            "search", channel_id=1, history=history,
            system_prompt="sys", tools_for_api=[],
            can_use_tool=_allow_all, execute_tool=execute,
            status_callback=_noop_status,
            incremental_text_callback=_capture,
            max_turns=5, stall_watchdog=watchdog, api_client=client,
            model="claude-opus-4-8", extra_betas=[],
        )
        joined = "\n".join(sent)
        # Leaked XML never reached the user; the prose before it did.
        assert "<invoke" not in joined and "function_calls" not in joined
        assert "了解です" in joined
        assert "done" in result.text
        # The text-format call was actually executed with the parsed input.
        execute.assert_awaited_once_with(
            "mcp__brave-search__brave_web_search", {"query": "Andrew Left verdict"}
        )
        # The result was fed back to the model (proxy-execution feedback).
        assert any(
            m["role"] == "user" and isinstance(m["content"], list)
            and m["content"] and isinstance(m["content"][0], dict)
            and "代理実行" in m["content"][0].get("text", "")
            and "検索結果データ" in m["content"][0].get("text", "")
            for m in history
        )

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

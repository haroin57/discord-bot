"""Tests for services/deep_research (Deep Research mode — ADR-0028).

Covers: source machine-extraction + dedup, the guarded MCP call wrapper,
run_subagent (fresh-context / bounded result / tool gating / source parsing via
run_agent_loop), and the orchestrator (busy rejection, web-backend precondition,
happy path with clarification timeout). All LLM/MCP/Discord I/O is mocked.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import services.deep_research.magi as magi
import services.deep_research.orchestrator as orch
import services.deep_research.registry as registry
from services.deep_research import sources as sources_mod
from services.deep_research.registry import ResearchAgentDef, _guarded_call_tool, run_subagent
from services.deep_research.types import LensResult, ResearchState, SubAgentResult


# ---------------------------------------------------------------------------
# types.ResearchState / sources.parse_sources
# ---------------------------------------------------------------------------
class TestSources:
    def test_register_source_dedup_and_sid(self):
        st = ResearchState(query="q")
        a = st.register_source(url="https://example.com/a", title="A")
        a2 = st.register_source(url="https://example.com/a/", title="A dup")  # trailing slash
        b = st.register_source(url="https://example.com/b#frag", title="B")   # fragment
        assert a.sid == "S1" and b.sid == "S2"
        assert a2 is a                       # deduped to the same SourceRef
        assert len(st.sources) == 2

    def test_register_source_rejects_non_http(self):
        st = ResearchState(query="q")
        assert st.register_source(url="ftp://x") is None
        assert st.register_source(url="") is None
        assert len(st.sources) == 0

    def test_parse_sources_from_brave_json(self):
        st = ResearchState(query="q")
        raw = json.dumps({
            "web": {"results": [
                {"title": "T1", "url": "https://x.com/1", "description": "d1"},
                {"title": "T2", "url": "https://y.com/2"},
            ]},
        })
        refs = sources_mod.parse_sources("mcp__brave-search__brave_web_search", raw, st)
        assert [r.sid for r in refs] == ["S1", "S2"]
        assert st.sources[next(iter(st.sources))].source_type == "web"

    def test_parse_sources_x_type(self):
        st = ResearchState(query="q")
        raw = json.dumps([{"url": "https://x.com/p/1", "text": "tweet"}])
        refs = sources_mod.parse_sources("mcp__x-scraper__search_x", raw, st)
        assert refs[0].source_type == "x"

    def test_parse_sources_text_fallback(self):
        st = ResearchState(query="q")
        raw = "see https://foo.example/page and https://bar.example/x)."
        refs = sources_mod.parse_sources("mcp__tavily__tavily_search", raw, st)
        urls = {r.url for r in refs}
        assert "https://foo.example/page" in urls
        assert "https://bar.example/x" in urls   # trailing ). stripped

    def test_parse_sources_malformed_is_safe(self):
        st = ResearchState(query="q")
        assert sources_mod.parse_sources("mcp__brave-search__x", "<<not json>>", st) == []

    def test_add_usage_accumulates(self):
        st = ResearchState(query="q")
        st.add_usage({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10})
        st.add_usage({"output_tokens": 5})
        assert st.tokens_used == 165

    def test_verify_citations_flags_missing(self):
        st = ResearchState(query="q")
        st.register_source(url="https://a.com")  # S1
        from services.deep_research.types import SubAgentResult
        st.results.append(SubAgentResult(subq="x", summary_md="", cited_sids=["S1", "S9"]))
        assert sources_mod.verify_citations(st) == ["S9"]


# ---------------------------------------------------------------------------
# registry._guarded_call_tool / run_subagent
# ---------------------------------------------------------------------------
class TestGuardedCallTool:
    @pytest.mark.asyncio
    async def test_creates_server_lock_and_calls(self):
        pool = MagicMock()
        pool.call_tool = AsyncMock(return_value="result")
        server_locks: dict = {}
        out = await _guarded_call_tool(pool, server_locks, "mcp__brave-search__brave_web_search", {"q": "x"})
        assert out == "result"
        assert "brave-search" in server_locks          # per-server lock registered
        pool.call_tool.assert_awaited_once()


class TestRunSubagent:
    @pytest.mark.asyncio
    async def test_fresh_context_bounded_and_gating(self):
        captured = {}

        async def fake_loop(prompt, channel_id, **kwargs):
            captured["prompt"] = prompt
            captured["channel_id"] = channel_id
            captured.update(kwargs)
            res = MagicMock()
            res.text = "x" * 200_000          # oversized -> must be capped
            res.usage_total = {"output_tokens": 42}
            return res

        pool = MagicMock()
        pool.call_tool = AsyncMock(return_value=json.dumps({"url": "https://s.com/1", "title": "S"}))
        defn = ResearchAgentDef(
            agent_type="researcher", model="claude-opus-4-8",
            system_prompt="SYS", max_tool_turns=5,
        )
        state = ResearchState(query="q")
        tools = [{"name": "mcp__brave-search__brave_web_search", "input_schema": {}}]

        with patch.object(registry, "run_agent_loop", new=fake_loop):
            result = await run_subagent(
                api_client=MagicMock(), pool=pool, server_locks={},
                defn=defn, briefing="BRIEF", tools_for_api=tools,
                state=state, channel_id=999,
            )

        # fresh context: history is an empty list passed in
        assert captured["history"] == []
        assert captured["system_prompt"] == "SYS"
        assert captured["model"] == "claude-opus-4-8"
        assert captured["max_turns"] == 5
        assert captured["tools_for_api"] is tools
        # bounded result (CLI maxResultSizeChars)
        assert len(result.summary_md) == 100_000
        assert result.ok is True
        assert state.tokens_used == 42

        # tool gating + source parsing happen via the wired execute_tool
        can_use_tool = captured["can_use_tool"]
        execute_tool = captured["execute_tool"]
        assert can_use_tool("mcp__brave-search__brave_web_search", {})[0] is True
        assert can_use_tool("Bash", {})[0] is False
        raw = await execute_tool("mcp__brave-search__brave_web_search", {"q": "x"})
        assert "s.com/1" in raw
        assert len(state.sources) == 1            # parse_sources ran inside execute_tool

    def test_is_transient_api_error_classification(self):
        # 一時的（リトライ対象）: 5xx / 529 / 401（OAuth refresh で復活する可能性）
        for status in (529, 500, 502, 503, 504, 401):
            e = MagicMock(); e.status_code = status
            assert registry._is_transient_api_error(e) is True, f"status={status} should be transient"
        e_text = Exception("overloaded_error: Overloaded")
        assert registry._is_transient_api_error(e_text) is True
        e_auth = Exception("authentication_error: Invalid authentication credentials")
        assert registry._is_transient_api_error(e_auth) is True
        # 一時的でない（即失敗）: 429（quota）/ 400・403・404（contract / forbidden / not found）
        for status in (400, 403, 404, 429):
            e = MagicMock(); e.status_code = status
            assert registry._is_transient_api_error(e) is False, f"status={status} should NOT be transient"

    @pytest.mark.asyncio
    async def test_run_subagent_refreshes_oauth_token_before_each_retry(self):
        # rid 9e974424 の回帰: 401 → refresh → 2 回目で復活、を担保する。
        calls = {"loop": 0, "ensure": 0}

        async def fake_loop(prompt, channel_id, **kwargs):
            calls["loop"] += 1
            if calls["loop"] == 1:
                exc = type("APIStatusError", (Exception,), {})("401 authentication_error")
                exc.status_code = 401
                raise exc
            res = MagicMock(); res.text = "ok"; res.usage_total = None
            return res

        async def fake_ensure():
            calls["ensure"] += 1

        defn = ResearchAgentDef(agent_type="integrator", model="m", system_prompt="s", max_tool_turns=1)
        # 動的 import を差し替えるため、services.oauth_manager にモジュール属性を当てる
        import services.oauth_manager as om
        with patch.object(registry, "run_agent_loop", new=fake_loop), \
             patch.object(registry, "_TRANSIENT_BACKOFF_BASE_S", 0), \
             patch.object(om, "ensure_valid_token", new=fake_ensure):
            result = await run_subagent(
                api_client=MagicMock(), pool=MagicMock(), server_locks={},
                defn=defn, briefing="b", tools_for_api=[],
                state=ResearchState(query="q"), channel_id=1,
            )
        assert result.ok is True
        assert calls["loop"] == 2          # 401 → リトライして 2 回目で成功
        # ensure_valid_token は各試行の直前に呼ばれる（refresh で 401 を救う狙い）
        assert calls["ensure"] == 2

    @pytest.mark.asyncio
    async def test_run_subagent_retries_on_overloaded(self):
        # overloaded で1回失敗 → リトライ → 2回目で成功（rid 5fcf8755 のような全滅を防ぐ）。
        calls = {"n": 0}

        async def fake_loop(prompt, channel_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                exc = type("APIStatusError", (Exception,), {})("overloaded_error: Overloaded")
                exc.status_code = 529
                raise exc
            res = MagicMock(); res.text = "ok"; res.usage_total = None
            return res

        defn = ResearchAgentDef(agent_type="planner", model="m", system_prompt="s", max_tool_turns=1)
        with patch.object(registry, "run_agent_loop", new=fake_loop), \
             patch.object(registry, "_TRANSIENT_BACKOFF_BASE_S", 0):
            result = await run_subagent(
                api_client=MagicMock(), pool=MagicMock(), server_locks={},
                defn=defn, briefing="b", tools_for_api=[],
                state=ResearchState(query="q"), channel_id=1,
            )
        assert result.ok is True
        assert calls["n"] == 2          # 1回目失敗 → リトライして成功

    @pytest.mark.asyncio
    async def test_run_subagent_no_retry_on_non_transient(self):
        # 429（quota）はリトライしない（無駄な待ちを避ける）。
        calls = {"n": 0}

        async def fake_loop(prompt, channel_id, **kwargs):
            calls["n"] += 1
            exc = type("APIStatusError", (Exception,), {})("rate_limit_error: usage credits")
            exc.status_code = 429
            raise exc

        defn = ResearchAgentDef(agent_type="searcher", model="m", system_prompt="s", max_tool_turns=1)
        with patch.object(registry, "run_agent_loop", new=fake_loop), \
             patch.object(registry, "_TRANSIENT_BACKOFF_BASE_S", 0):
            result = await run_subagent(
                api_client=MagicMock(), pool=MagicMock(), server_locks={},
                defn=defn, briefing="b", tools_for_api=[],
                state=ResearchState(query="q"), channel_id=1,
            )
        assert result.ok is False
        assert calls["n"] == 1          # リトライせず即失敗

    def test_compact_briefing_within_budget(self):
        # 予算内ならそのまま返す
        out = registry._compact_briefing("sys", "x" * 100, budget_chars=1000)
        assert out == "x" * 100

    def test_compact_briefing_truncates_when_over(self):
        sys = "S" * 100
        brief = "B" * 1000
        out = registry._compact_briefing(sys, brief, budget_chars=500)
        # system はそのまま、briefing は (500-100)=400 以下に切られて省略マーカー付き
        assert "省略しました" in out
        assert out.startswith("B" * 400)

    def test_compact_briefing_system_alone_exceeds_budget(self):
        # system だけで予算を超えるケース: briefing は丸ごと省略マーカーに置換
        out = registry._compact_briefing("S" * 1000, "B" * 500, budget_chars=500)
        assert "全体を省略" in out

    @pytest.mark.asyncio
    async def test_subagent_filters_context_1m_beta(self):
        # context-1m beta は subagent では外して送信される（rid 9e974424 対策）
        captured = {}

        async def fake_loop(prompt, channel_id, **kwargs):
            captured.update(kwargs)
            res = MagicMock(); res.text = "ok"; res.usage_total = None
            return res

        defn = ResearchAgentDef(agent_type="searcher", model="m", system_prompt="s", max_tool_turns=1)
        with patch.object(registry, "run_agent_loop", new=fake_loop):
            await run_subagent(
                api_client=MagicMock(), pool=MagicMock(), server_locks={},
                defn=defn, briefing="b", tools_for_api=[],
                state=ResearchState(query="q"), channel_id=1,
            )
        betas = captured["extra_betas"]
        # context-1m は除外、それ以外は残る（oauth, prompt-caching 等の必須 beta は維持）
        assert not any(b.startswith("context-1m") for b in betas)
        assert "oauth-2025-04-20" in betas

    @pytest.mark.asyncio
    async def test_subagent_compacts_briefing_at_dispatch(self):
        # 巨大な briefing は run_agent_loop に渡る前に圧縮される。
        captured = {}

        async def fake_loop(prompt, channel_id, **kwargs):
            captured["prompt"] = prompt
            res = MagicMock(); res.text = "ok"; res.usage_total = None
            return res

        defn = ResearchAgentDef(agent_type="searcher", model="m", system_prompt="s", max_tool_turns=1)
        huge_briefing = "Q" * 800_000   # 540K char budget を超える
        with patch.object(registry, "run_agent_loop", new=fake_loop):
            await run_subagent(
                api_client=MagicMock(), pool=MagicMock(), server_locks={},
                defn=defn, briefing=huge_briefing, tools_for_api=[],
                state=ResearchState(query="q"), channel_id=1,
            )
        assert len(captured["prompt"]) < 800_000
        assert "省略しました" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_huge_tool_result_bounded_but_sources_fully_extracted(self):
        # 巨大な MCP 戻り値はモデル context 用に有界化するが、ソース抽出は全文から行う
        # （200k 超→context-1m long-context 課金 429 を防ぐ。通常chatの microcompact 相当）。
        big = json.dumps([
            {"url": f"https://s.com/{i}", "title": f"T{i}", "text": "x" * 500}
            for i in range(200)
        ])
        assert len(big) > registry.DEEP_RESEARCH_MAX_TOOL_RESULT_CHARS

        async def fake_loop(prompt, channel_id, **kwargs):
            captured = kwargs
            out = await captured["execute_tool"]("mcp__brave-search__brave_web_search", {"q": "x"})
            res = MagicMock(); res.text = "ok"; res.usage_total = None
            res._out = out
            fake_loop.captured_out = out
            return res

        pool = MagicMock()
        pool.call_tool = AsyncMock(return_value=big)
        defn = ResearchAgentDef(agent_type="searcher", model="m", system_prompt="s", max_tool_turns=1)
        state = ResearchState(query="q")
        with patch.object(registry, "run_agent_loop", new=fake_loop):
            await run_subagent(
                api_client=MagicMock(), pool=pool, server_locks={},
                defn=defn, briefing="b",
                tools_for_api=[{"name": "mcp__brave-search__brave_web_search"}],
                state=state, channel_id=1,
            )
        out = fake_loop.captured_out
        # モデルに渡る本文は上限内に収まり、省略マーカーが付く
        assert len(out) <= registry.DEEP_RESEARCH_MAX_TOOL_RESULT_CHARS + 200
        assert "省略" in out
        # ソースは全文（200件）から抽出されている（切り詰めの影響を受けない）
        assert len(state.sources) == 200

    @pytest.mark.asyncio
    async def test_tool_error_surfaces_as_tool_use_error(self):
        async def fake_loop(prompt, channel_id, **kwargs):
            captured = kwargs
            # invoke execute_tool with a pool that raises
            out = await captured["execute_tool"]("mcp__brave-search__x", {})
            res = MagicMock(); res.text = out; res.usage_total = None
            return res

        pool = MagicMock()
        pool.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())
        defn = ResearchAgentDef(agent_type="r", model="m", system_prompt="s", max_tool_turns=1)
        with patch.object(registry, "run_agent_loop", new=fake_loop):
            result = await run_subagent(
                api_client=MagicMock(), pool=pool, server_locks={},
                defn=defn, briefing="b",
                tools_for_api=[{"name": "mcp__brave-search__x"}],
                state=ResearchState(query="q"), channel_id=1,
            )
        assert "tool_use_error" in result.summary_md
        assert "timed out" in result.summary_md

    @pytest.mark.asyncio
    async def test_loop_exception_returns_not_ok(self):
        async def boom(*a, **k):
            raise RuntimeError("kaboom")
        defn = ResearchAgentDef(agent_type="r", model="m", system_prompt="s", max_tool_turns=1)
        with patch.object(registry, "run_agent_loop", new=boom):
            result = await run_subagent(
                api_client=MagicMock(), pool=MagicMock(), server_locks={},
                defn=defn, briefing="b", tools_for_api=[],
                state=ResearchState(query="q"), channel_id=1,
            )
        assert result.ok is False
        assert "kaboom" in (result.error or "")


# ---------------------------------------------------------------------------
# orchestrator.run_deep_research
# ---------------------------------------------------------------------------
def _make_deps():
    bot = MagicMock()
    bot.wait_for = AsyncMock(side_effect=asyncio.TimeoutError())  # clarify times out by default
    return orch.DeepResearchDeps(
        bot=bot,
        send_segmented=AsyncMock(),
        get_http_session=AsyncMock(),
        track_task=MagicMock(),
    )


def _make_channel():
    channel = MagicMock()
    anchor = MagicMock()
    anchor.edit = AsyncMock()
    thread = MagicMock()
    thread.id = 555000
    thread.send = AsyncMock(return_value=anchor)
    origin = MagicMock()
    origin.create_thread = AsyncMock(return_value=thread)
    channel.send = AsyncMock(return_value=origin)
    return channel, origin, thread, anchor


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_busy_rejects(self):
        orch._research_busy = True
        try:
            channel, *_ = _make_channel()
            await orch.run_deep_research(channel, MagicMock(id=1), "q", _make_deps())
            channel.send.assert_awaited()           # busy notice
            channel.send.return_value.create_thread.assert_not_called()
        finally:
            orch._research_busy = False

    @pytest.mark.asyncio
    async def test_precondition_no_web_backend(self):
        channel, origin, thread, anchor = _make_channel()
        with patch.object(orch, "MCP_SERVERS_WEB", {"x-scraper": {}}), \
             patch.object(orch, "run_subagent", new=AsyncMock()) as rsa:
            await orch.run_deep_research(channel, MagicMock(id=1), "q", _make_deps(), skip_clarify=True)
        rsa.assert_not_awaited()                     # never ran the searcher
        # anchor edited with the precondition error (content= kwarg)
        assert any("brave" in str(c.kwargs.get("content", "")) for c in anchor.edit.call_args_list)

    @pytest.mark.asyncio
    async def test_happy_path_single_agent(self):
        from services.deep_research.types import SubAgentResult
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()

        fake_pool = MagicMock()
        fake_pool.spawn_eager = AsyncMock()
        fake_pool.handles = {}
        tools = [{"name": "mcp__brave-search__brave_web_search", "input_schema": {}}]

        with patch.object(orch, "DEEP_RESEARCH_MULTI_AGENT", False), \
             patch.object(orch, "MCP_SERVERS_WEB", {"brave-search": {"x": 1}, "x-scraper": {}}), \
             patch.object(orch, "make_api_client", new=AsyncMock(return_value=MagicMock())), \
             patch.object(orch, "McpPool", return_value=fake_pool), \
             patch.object(orch, "mcp_tools_to_anthropic", return_value=tools), \
             patch.object(orch, "run_subagent", new=AsyncMock(
                 return_value=SubAgentResult(subq="b", summary_md="# レポート本文", ok=True),
             )) as rsa:
            await orch.run_deep_research(
                channel, MagicMock(id=1), "日経VIの現状", deps, skip_clarify=True,
            )

        channel.send.assert_awaited()                # origin message
        origin.create_thread.assert_awaited_once()   # message-anchored thread
        thread.send.assert_awaited()                 # anchor in thread
        rsa.assert_awaited_once()
        # report delivered into the thread via the shared segmented sender
        deps.send_segmented.assert_awaited_once()
        sent_anchor, sent_text, sent_files = deps.send_segmented.call_args.args
        assert sent_text == "# レポート本文"

    @pytest.mark.asyncio
    async def test_clarify_reaction_skips_to_default_scope(self):
        # 時間制限は廃止。リアクションで「確認なし・既定スコープ続行」（タイムアウトに依存しない）。
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()

        async def _wf(event, *, check=None, timeout=None):
            assert timeout is None              # 明確化に時間制限を渡さない
            if event == "raw_reaction_add":
                return SimpleNamespace(message_id=1, user_id=1)
            await asyncio.Event().wait()        # message: 無制限に待つ（来ない）
        deps.bot.wait_for = _wf
        deps.bot.user = SimpleNamespace(id=999)
        fake_pool = MagicMock(); fake_pool.spawn_eager = AsyncMock(); fake_pool.handles = {}
        tools = [{"name": "mcp__brave-search__b", "input_schema": {}}]

        async def fake_clarify_loop(prompt, channel_id, **kwargs):
            res = MagicMock(); res.text = "- 期間は？\n- 地域は？"; res.usage_total = None
            return res

        with patch.object(orch, "DEEP_RESEARCH_MULTI_AGENT", False), \
             patch.object(orch, "MCP_SERVERS_WEB", {"brave-search": {"x": 1}}), \
             patch.object(orch, "make_api_client", new=AsyncMock(return_value=MagicMock())), \
             patch.object(orch, "McpPool", return_value=fake_pool), \
             patch.object(orch, "mcp_tools_to_anthropic", return_value=tools), \
             patch.object(orch, "run_agent_loop", new=fake_clarify_loop), \
             patch.object(orch, "run_subagent", new=AsyncMock(
                 return_value=SubAgentResult(subq="b", summary_md="R", ok=True),
             )):
            await orch.run_deep_research(channel, MagicMock(id=1), "q", deps, skip_clarify=False)

        sends = [str(c.args[0]) for c in thread.send.call_args_list]
        assert any("確認" in s and "時間制限はありません" in s for s in sends)
        assert any("既定スコープ" in s for s in sends)

    @pytest.mark.asyncio
    async def test_clarify_reply_with_auto_mention_still_folded(self):
        # 回帰: Discord「返信」は元発言者(=bot)を自動メンションする。clarify はメンションを
        # スキップ扱いにせず、本文があれば必ず畳み込む（answer-vs-skip の二択）。
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        bot_user = SimpleNamespace(id=999)
        deps.bot.user = bot_user
        reply = SimpleNamespace(
            content="直近6ヶ月・日本SE市場で。学習投資は技術書/講座を含む",
            mentions=[bot_user],   # ← Reply による auto-mention（バグの再現条件）
            reference=SimpleNamespace(message_id=12345),
            channel=SimpleNamespace(id=555000),
            author=SimpleNamespace(id=1, bot=False),
        )

        async def _wf(event, *, check=None, timeout=None):
            if event == "message":
                return reply
            await asyncio.Event().wait()
        deps.bot.wait_for = _wf
        fake_pool = MagicMock(); fake_pool.spawn_eager = AsyncMock(); fake_pool.handles = {}
        tools = [{"name": "mcp__brave-search__b", "input_schema": {}}]
        captured = {}

        async def fake_clarify_loop(prompt, channel_id, **kwargs):
            res = MagicMock(); res.text = "- 期間は？\n- 対象市場は？"; res.usage_total = None
            return res

        async def fake_sub(**kw):
            captured["clarified"] = kw["state"].clarified_query
            return SubAgentResult("r", "**レポート**", ok=True)

        with patch.object(orch, "DEEP_RESEARCH_MULTI_AGENT", False), \
             patch.object(orch, "MCP_SERVERS_WEB", {"brave-search": {"x": 1}}), \
             patch.object(orch, "make_api_client", new=AsyncMock(return_value=MagicMock())), \
             patch.object(orch, "McpPool", return_value=fake_pool), \
             patch.object(orch, "mcp_tools_to_anthropic", return_value=tools), \
             patch.object(orch, "run_agent_loop", new=fake_clarify_loop), \
             patch.object(orch, "run_subagent", new=fake_sub):
            await orch.run_deep_research(channel, MagicMock(id=1), "SE 投資", deps, skip_clarify=False)

        # 回答が clarified_query に必ず畳み込まれる（auto-mention で approve に化けない）
        assert "直近6ヶ月" in captured["clarified"]
        sends = [str(c.args[0]) for c in thread.send.call_args_list]
        assert any("補足を反映" in s for s in sends)
        assert not any("既定スコープで進めます" in s for s in sends)

    @pytest.mark.asyncio
    async def test_clarify_reply_folds_into_query(self):
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        reply = SimpleNamespace(
            content="直近3ヶ月・日本市場で", mentions=[],
            channel=SimpleNamespace(id=555000), author=SimpleNamespace(id=1, bot=False),
        )

        async def _wf(event, *, check=None, timeout=None):
            if event == "message":
                return reply
            await asyncio.Event().wait()        # reaction: 来ない（無制限）
        deps.bot.wait_for = _wf
        deps.bot.user = SimpleNamespace(id=999)
        fake_pool = MagicMock(); fake_pool.spawn_eager = AsyncMock(); fake_pool.handles = {}
        tools = [{"name": "mcp__brave-search__b", "input_schema": {}}]
        captured = {}

        async def fake_clarify_loop(prompt, channel_id, **kwargs):
            res = MagicMock(); res.text = "- 期間は？"; res.usage_total = None
            return res

        async def fake_sub(**kw):
            captured["clarified"] = kw["state"].clarified_query
            return SubAgentResult("r", "**レポート**", ok=True)

        with patch.object(orch, "DEEP_RESEARCH_MULTI_AGENT", False), \
             patch.object(orch, "MCP_SERVERS_WEB", {"brave-search": {"x": 1}}), \
             patch.object(orch, "make_api_client", new=AsyncMock(return_value=MagicMock())), \
             patch.object(orch, "McpPool", return_value=fake_pool), \
             patch.object(orch, "mcp_tools_to_anthropic", return_value=tools), \
             patch.object(orch, "run_agent_loop", new=fake_clarify_loop), \
             patch.object(orch, "run_subagent", new=fake_sub):
            await orch.run_deep_research(channel, MagicMock(id=1), "日経VI", deps, skip_clarify=False)

        # 返信が clarified_query に畳み込まれて調査に渡る
        assert "直近3ヶ月" in captured["clarified"]
        assert any("補足を反映" in str(c.args[0]) for c in thread.send.call_args_list)


# ---------------------------------------------------------------------------
# Phase 1: extract_plan + multi-agent orchestration
# ---------------------------------------------------------------------------
class TestExtractPlan:
    def test_json_array(self):
        from services.deep_research.registry import extract_plan
        assert extract_plan('["q1", "q2", "q3"]', 4) == ["q1", "q2", "q3"]

    def test_fenced_with_prose(self):
        from services.deep_research.registry import extract_plan
        assert extract_plan('plan:\n```json\n["a","b"]\n```', 4) == ["a", "b"]

    def test_bullet_fallback_and_dedup(self):
        from services.deep_research.registry import extract_plan
        out = extract_plan("- 最初の質問はこれ\n- 最初の質問はこれ\n2. 次の質問はあれ", 4)
        assert out == ["最初の質問はこれ", "次の質問はあれ"]

    def test_limit(self):
        from services.deep_research.registry import extract_plan
        assert extract_plan('["aaaaaa","bbbbbb","cccccc","dddddd"]', 2) == ["aaaaaa", "bbbbbb"]

    def test_empty(self):
        from services.deep_research.registry import extract_plan
        assert extract_plan("", 4) == []


class TestMultiAgent:
    def _patches(self, fake_subagent, fake_pool, tools):
        return [
            patch.object(orch, "DEEP_RESEARCH_MULTI_AGENT", True),
            patch.object(orch, "DEEP_RESEARCH_MAGI_ENABLED", False),  # synthesizer path
            patch.object(orch, "DEEP_RESEARCH_MAX_PARALLEL", 2),
            patch.object(orch, "DEEP_RESEARCH_MAX_SUBAGENTS", 4),
            patch.object(orch, "MCP_SERVERS_WEB", {"brave-search": {"x": 1}}),
            patch.object(orch, "make_api_client", new=AsyncMock(return_value=MagicMock())),
            patch.object(orch, "McpPool", return_value=fake_pool),
            patch.object(orch, "mcp_tools_to_anthropic", return_value=tools),
            patch.object(orch, "run_subagent", new=fake_subagent),
        ]

    @pytest.mark.asyncio
    async def test_plan_search_synthesize(self):
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        fake_pool = MagicMock(); fake_pool.spawn_eager = AsyncMock(); fake_pool.handles = {}
        tools = [{"name": "mcp__brave-search__b", "input_schema": {}}]
        calls = {"planner": 0, "searcher": 0, "synthesizer": 0}

        async def fake_subagent(**kw):
            defn = kw["defn"]
            state = kw["state"]
            calls[defn.agent_type] += 1
            if defn.agent_type == "planner":
                return SubAgentResult(subq="p", summary_md='["サブ質問1", "サブ質問2", "サブ質問3"]', ok=True)
            if defn.agent_type == "searcher":
                state.register_source(url=f"https://s.com/{calls['searcher']}", title="t")
                return SubAgentResult(subq=kw["briefing"], summary_md="findings...", ok=True)
            if defn.agent_type == "synthesizer":
                return SubAgentResult(subq="synth", summary_md="**最終レポート**", ok=True)
            return SubAgentResult(subq="?", summary_md="", ok=False)

        from contextlib import ExitStack
        with ExitStack() as es:
            for p in self._patches(fake_subagent, fake_pool, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        assert calls["planner"] == 1
        assert calls["searcher"] == 3          # 3 sub-questions
        assert calls["synthesizer"] == 1
        deps.send_segmented.assert_awaited_once()
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "**最終レポート**" in sent_text
        assert "情報ソース" in sent_text        # 機械収集ソースが決定的に付与される

    @pytest.mark.asyncio
    async def test_partial_searcher_failure_still_synthesizes(self):
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        fake_pool = MagicMock(); fake_pool.spawn_eager = AsyncMock(); fake_pool.handles = {}
        tools = [{"name": "mcp__brave-search__b", "input_schema": {}}]

        async def fake_subagent(**kw):
            defn = kw["defn"]
            if defn.agent_type == "planner":
                return SubAgentResult(subq="p", summary_md='["q1", "q2"]', ok=True)
            if defn.agent_type == "searcher":
                if "q2" in kw["briefing"]:
                    raise RuntimeError("searcher boom")   # captured by gather(return_exceptions)
                return SubAgentResult(subq=kw["briefing"], summary_md="ok findings", ok=True)
            if defn.agent_type == "synthesizer":
                return SubAgentResult(subq="synth", summary_md="**レポート**", ok=True)
            return SubAgentResult(subq="?", summary_md="", ok=False)

        from contextlib import ExitStack
        with ExitStack() as es:
            for p in self._patches(fake_subagent, fake_pool, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        # one searcher failed but synthesis still produced a report
        deps.send_segmented.assert_awaited_once()
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert sent_text == "**レポート**"

    @pytest.mark.asyncio
    async def test_plan_failure_falls_back_to_single_agent(self):
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        fake_pool = MagicMock(); fake_pool.spawn_eager = AsyncMock(); fake_pool.handles = {}
        tools = [{"name": "mcp__brave-search__b", "input_schema": {}}]
        seen = {"researcher": 0}

        async def fake_subagent(**kw):
            defn = kw["defn"]
            if defn.agent_type == "planner":
                return SubAgentResult(subq="p", summary_md="(計画できません)", ok=False)  # plan fails
            if defn.agent_type == "researcher":          # single-agent fallback
                seen["researcher"] += 1
                return SubAgentResult(subq="r", summary_md="**フォールバックレポート**", ok=True)
            return SubAgentResult(subq="?", summary_md="", ok=False)

        from contextlib import ExitStack
        with ExitStack() as es:
            for p in self._patches(fake_subagent, fake_pool, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        assert seen["researcher"] == 1                   # fell back to single-agent
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert sent_text == "**フォールバックレポート**"


# ---------------------------------------------------------------------------
# Phase 2: citation 厳密化
# ---------------------------------------------------------------------------
class TestCitation:
    def _state_with_sources(self):
        st = ResearchState(query="q")
        st.register_source(url="https://a.com/1", title="A")  # S1
        st.register_source(url="https://b.com/2", title="B")  # S2
        return st

    def test_extract_cited_sids_unique_ordered(self):
        assert sources_mod.extract_cited_sids("x [S2] y [S1] z [S2]") == ["S2", "S1"]

    def test_verify_report_citations(self):
        st = self._state_with_sources()
        present, missing = sources_mod.verify_report_citations("a [S1] b [S5]", st)
        assert present == ["S1"] and missing == ["S5"]

    def test_build_final_report_appends_cited_sources_and_warns(self):
        st = self._state_with_sources()
        out = sources_mod.build_final_report("本文 [S1] と [S5]", st)
        assert "**情報ソース**" in out
        assert "<https://a.com/1>" in out      # cited S1 listed
        assert "<https://b.com/2>" not in out  # S2 not cited -> not listed
        assert "⚠️" in out and "S5" in out      # hallucinated citation flagged

    def test_build_final_report_no_sources(self):
        st = ResearchState(query="q")
        assert sources_mod.build_final_report("本文のみ", st) == "本文のみ"

    def test_build_final_report_lists_all_when_no_citations(self):
        st = self._state_with_sources()
        out = sources_mod.build_final_report("引用なし本文", st)
        assert "<https://a.com/1>" in out and "<https://b.com/2>" in out


# ---------------------------------------------------------------------------
# Phase 3: 予算 degrade / synthesizer retry / 全 searcher 失敗時 lead retry
# ---------------------------------------------------------------------------
def _phase3_patches(fake_subagent, fake_pool, tools, extra=None):
    base = [
        patch.object(orch, "DEEP_RESEARCH_MULTI_AGENT", True),
        patch.object(orch, "DEEP_RESEARCH_MAGI_ENABLED", False),  # synthesizer path
        patch.object(orch, "DEEP_RESEARCH_MAX_PARALLEL", 2),
        patch.object(orch, "DEEP_RESEARCH_MAX_SUBAGENTS", 4),
        patch.object(orch, "MCP_SERVERS_WEB", {"brave-search": {"x": 1}}),
        patch.object(orch, "make_api_client", new=AsyncMock(return_value=MagicMock())),
        patch.object(orch, "McpPool", return_value=fake_pool),
        patch.object(orch, "mcp_tools_to_anthropic", return_value=tools),
        patch.object(orch, "run_subagent", new=fake_subagent),
    ]
    return base + (extra or [])


class TestPhase3:
    def _pool_tools(self):
        fp = MagicMock(); fp.spawn_eager = AsyncMock(); fp.handles = {}
        return fp, [{"name": "mcp__brave-search__b", "input_schema": {}}]

    @pytest.mark.asyncio
    async def test_budget_degrade_skips_synthesizer(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        fp, tools = self._pool_tools()
        synth_calls = []

        async def fake(**kw):
            d = kw["defn"]; st = kw["state"]
            if d.agent_type == "planner":
                return SubAgentResult("p", '["q1", "q2"]', ok=True)
            if d.agent_type == "searcher":
                st.register_source(url="https://x.com/1", title="X")
                return SubAgentResult(kw["briefing"], "findings", ok=True)
            if d.agent_type == "synthesizer":
                synth_calls.append(1)
                return SubAgentResult("s", "should not run", ok=True)
            return SubAgentResult("?", "", ok=False)

        extra = [patch.object(orch, "DEEP_RESEARCH_TOTAL_TIME_BUDGET_S", 0)]
        with ExitStack() as es:
            for p in _phase3_patches(fake, fp, tools, extra):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        assert synth_calls == []                       # synthesizer skipped (over budget)
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "簡易版" in sent_text                    # degraded report

    @pytest.mark.asyncio
    async def test_synthesizer_retry_on_transient_failure(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        fp, tools = self._pool_tools()
        synth_n = {"n": 0}

        async def fake(**kw):
            d = kw["defn"]
            if d.agent_type == "planner":
                return SubAgentResult("p", '["q1"]', ok=True)
            if d.agent_type == "searcher":
                return SubAgentResult(kw["briefing"], "f", ok=True)
            if d.agent_type == "synthesizer":
                synth_n["n"] += 1
                if synth_n["n"] == 1:
                    return SubAgentResult("s", "", ok=False, error="429 overloaded")
                return SubAgentResult("s", "**やり直しレポート**", ok=True)
            return SubAgentResult("?", "", ok=False)

        extra = [patch.object(orch, "_SYNTH_RETRY_BACKOFF_S", 0)]
        with ExitStack() as es:
            for p in _phase3_patches(fake, fp, tools, extra):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        assert synth_n["n"] == 2                        # retried once
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "やり直しレポート" in sent_text

    @pytest.mark.asyncio
    async def test_all_searchers_fail_retries_with_lead_model(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        fp, tools = self._pool_tools()
        rounds = {"searcher": 0}

        async def fake(**kw):
            d = kw["defn"]
            if d.agent_type == "planner":
                return SubAgentResult("p", '["q1", "q2"]', ok=True)
            if d.agent_type == "searcher":
                rounds["searcher"] += 1
                if kw.get("model_override"):           # lead-model retry round
                    return SubAgentResult(kw["briefing"], "recovered", ok=True)
                return SubAgentResult(kw["briefing"], "", ok=False, error="model not found")
            if d.agent_type == "synthesizer":
                return SubAgentResult("s", "**復活レポート**", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _phase3_patches(fake, fp, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        assert rounds["searcher"] == 4                 # 2 subqs × 2 rounds (initial + lead retry)
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "復活レポート" in sent_text


# ---------------------------------------------------------------------------
# A: active research thread suppression (二重処理バグ修正)
# ---------------------------------------------------------------------------
class TestActiveThread:
    def test_mark_unmark(self):
        orch._active_threads.clear()
        assert orch.is_active_research_thread(42) is False
        orch._mark_active(42)
        assert orch.is_active_research_thread(42) is True
        orch._unmark_active(42)
        assert orch.is_active_research_thread(42) is False

    @pytest.mark.asyncio
    async def test_thread_active_during_run_and_cleared_after(self):
        orch._active_threads.clear()
        channel, origin, thread, anchor = _make_channel()   # thread.id = 555000
        deps = _make_deps()
        fp = MagicMock(); fp.spawn_eager = AsyncMock(); fp.handles = {}
        tools = [{"name": "mcp__brave-search__b", "input_schema": {}}]
        seen = {}

        async def fake(**kw):
            seen["active_during"] = orch.is_active_research_thread(555000)
            return SubAgentResult("r", "**レポート**", ok=True)

        with patch.object(orch, "DEEP_RESEARCH_MULTI_AGENT", False), \
             patch.object(orch, "MCP_SERVERS_WEB", {"brave-search": {"x": 1}}), \
             patch.object(orch, "make_api_client", new=AsyncMock(return_value=MagicMock())), \
             patch.object(orch, "McpPool", return_value=fp), \
             patch.object(orch, "mcp_tools_to_anthropic", return_value=tools), \
             patch.object(orch, "run_subagent", new=fake):
            await orch.run_deep_research(channel, MagicMock(id=1), "q", deps, skip_clarify=True)

        assert seen["active_during"] is True                 # suppressed on_message during run
        assert orch.is_active_research_thread(555000) is False  # cleared in finally


# ---------------------------------------------------------------------------
# B: per-searcher individual messages + tool callback wiring
# ---------------------------------------------------------------------------
class TestSearcherViews:
    @pytest.mark.asyncio
    async def test_per_searcher_messages_and_tool_callback(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_deps()
        fp = MagicMock(); fp.spawn_eager = AsyncMock(); fp.handles = {}
        tools = [{"name": "mcp__brave-search__b", "input_schema": {}}]
        cbs = []

        async def fake(**kw):
            d = kw["defn"]
            if d.agent_type == "planner":
                return SubAgentResult("p", '["q1", "q2"]', ok=True)
            if d.agent_type == "searcher":
                cbs.append(kw.get("tool_invocation_callback"))
                return SubAgentResult(kw["briefing"], "findings", ok=True)
            if d.agent_type == "synthesizer":
                return SubAgentResult("s", "**R**", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _phase3_patches(fake, fp, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        # each searcher got its own _SearcherView.on_tool as tool_invocation_callback
        assert len(cbs) == 2
        for cb in cbs:
            assert cb is not None
            assert cb.__self__.__class__.__name__ == "_SearcherView"
        # anchor + 2 per-searcher dashboard messages were sent to the thread
        assert thread.send.await_count >= 3

    @pytest.mark.asyncio
    async def test_searcher_view_on_tool_edits_own_message(self):
        view_msg = MagicMock()
        view_msg.edit = AsyncMock()
        view = orch._SearcherView(1, 3, "サブ質問", view_msg)
        await view.on_tool("mcp__brave-search__brave_web_search", {"query": "日経VI"})
        view_msg.edit.assert_awaited()
        sent = view_msg.edit.call_args.kwargs["content"]
        assert "brave_web_search" in sent and "日経VI" in sent
        await view.finish(True)
        assert "✅" in view_msg.edit.call_args.kwargs["content"]


# ---------------------------------------------------------------------------
# MAGI 推論層（ADR-0030）: effort 配線 / 中心的問い / 入れ子レンズ / 統合 / 匿名化
# ---------------------------------------------------------------------------
class TestEffortThreading:
    """run_subagent が defn.effort を output_config_override として run_agent_loop に渡す。"""

    @pytest.mark.asyncio
    async def test_effort_passed_as_output_config_override(self):
        captured = {}

        async def fake_loop(prompt, channel_id, **kwargs):
            captured.update(kwargs)
            res = MagicMock(); res.text = "ok"; res.usage_total = None
            return res

        defn = ResearchAgentDef(
            agent_type="magi_lead", model="claude-opus-4-8",
            system_prompt="s", max_tool_turns=1, effort="max",
        )
        with patch.object(registry, "run_agent_loop", new=fake_loop):
            await run_subagent(
                api_client=MagicMock(), pool=MagicMock(), server_locks={},
                defn=defn, briefing="b", tools_for_api=[],
                state=ResearchState(query="q"), channel_id=1,
            )
        assert captured["output_config_override"] == {"effort": "max"}

    @pytest.mark.asyncio
    async def test_no_effort_means_no_override(self):
        captured = {}

        async def fake_loop(prompt, channel_id, **kwargs):
            captured.update(kwargs)
            res = MagicMock(); res.text = "ok"; res.usage_total = None
            return res

        defn = ResearchAgentDef(agent_type="searcher", model="m", system_prompt="s", max_tool_turns=1)
        with patch.object(registry, "run_agent_loop", new=fake_loop):
            await run_subagent(
                api_client=MagicMock(), pool=MagicMock(), server_locks={},
                defn=defn, briefing="b", tools_for_api=[],
                state=ResearchState(query="q"), channel_id=1,
            )
        assert captured["output_config_override"] is None


class TestCentralQuestionParsing:
    def test_json_object_with_cq_and_subqs(self):
        cq, subqs = registry.extract_central_question_and_plan(
            '{"central_question": "本質は何か", "subquestions": ["q1", "q2"]}', 4,
        )
        assert cq == "本質は何か"
        assert subqs == ["q1", "q2"]

    def test_falls_back_to_array_with_empty_cq(self):
        cq, subqs = registry.extract_central_question_and_plan('["a", "b"]', 4)
        assert cq == ""                      # CQ は Stage2 で埋まる
        assert subqs == ["a", "b"]

    def test_refined_cq_parser(self):
        cq, rationale = orch._parse_refined_cq("中心的問い: 研いだ問い\n核心: 事実の緊張に根ざす")
        assert cq == "研いだ問い"
        assert rationale == "事実の緊張に根ざす"

    def test_refined_cq_parser_value_on_next_line(self):
        # 実バグ再現: モデルが「中心的問い:」の次行に本文を書くと、旧実装はラベル
        # 行そのものを拾って空欄化していた。次行本文を正しく抽出すること。
        text = (
            "中心的問い:\n"
            "現在のAIブームはバブルであり、2026年6月の急落はその崩壊の始まりか？\n"
            "核心:\n"
            "過熱と急落の緊張に根ざす"
        )
        cq, rationale = orch._parse_refined_cq(text)
        assert cq == "現在のAIブームはバブルであり、2026年6月の急落はその崩壊の始まりか？"
        assert rationale == "過熱と急落の緊張に根ざす"
        assert cq != "中心的問い:"        # ラベル退行を起こさない

    def test_refined_cq_parser_label_only_is_blank(self):
        cq, _ = orch._parse_refined_cq("中心的問い:\n核心:")
        assert orch._meaningful_cq(cq) is False

    def test_meaningful_cq(self):
        assert orch._meaningful_cq("中心的問い:") is False
        assert orch._meaningful_cq("：　") is False
        assert orch._meaningful_cq("") is False
        assert orch._meaningful_cq("AIブームはバブルか？") is True


class TestLensParseAnonymize:
    def test_parse_lens_output_stance(self):
        assert magi.parse_lens_output("STANCE: 私の立場\n【手堅い結論】\n- x") == "私の立場"

    def test_parse_lens_output_fallback(self):
        assert magi.parse_lens_output("結論からいうと真だ\n根拠は…").startswith("結論からいうと真だ")

    def test_anonymize_hides_lens_identity(self):
        lenses = [
            LensResult("science", "科学（メルキオール）", "S", "科学の本文 [S1]", ok=True),
            LensResult("intuition", "直観（バルタザール）", "I", "直観の本文 [S2]", ok=True),
            LensResult("skeptic", "懐疑（カスパー）", "K", "懐疑の本文 [S3]", ok=True),
        ]
        out = magi.anonymize(lenses)
        assert "視点A" in out and "視点B" in out and "視点C" in out
        # lens identity（神名）は伏せられる
        assert "メルキオール" not in out
        assert "バルタザール" not in out
        assert "カスパー" not in out
        # 中身（本文）は保持される
        assert "科学の本文" in out and "直観の本文" in out

    def test_anonymize_skips_failed_lenses(self):
        lenses = [
            LensResult("science", "科学", "S", "生きてる [S1]", ok=True),
            LensResult("intuition", "直観", "", "", ok=False, error="boom"),
        ]
        out = magi.anonymize(lenses)
        assert "視点A" in out and "視点B" not in out


class TestSubagentParallelismFloor:
    """探索フェーズで「必ず N 並列」を担保するため、planner には最低数を明示要求する。"""

    def test_build_plan_briefing_carries_min_floor(self):
        from services.deep_research.prompts import build_plan_briefing
        b = build_plan_briefing("テーマ", max_subagents=6, min_subagents=5)
        assert "最低 5 個必須" in b
        assert "5〜6 個" in b
        assert "並列に検索" in b
        # 旧フォーマット（min=1）は文言を変えない
        b1 = build_plan_briefing("テーマ", max_subagents=4, min_subagents=1)
        assert "最低" not in b1
        assert "最大 4 個" in b1

    def test_build_plan_magi_briefing_carries_min_floor(self):
        from services.deep_research.prompts import build_plan_magi_briefing
        b = build_plan_magi_briefing("テーマ", max_subagents=6, min_subagents=5)
        assert "最低 5 個必須" in b
        assert "5〜6 個" in b

    @pytest.mark.asyncio
    async def test_plan_completion_when_planner_underdelivers(self):
        # 回帰: rid 9e974424 で planner が 1 個しか返さなかった → 補完呼び出しで MIN を満たす
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_magi_deps(_approve_by_reaction())
        fp = MagicMock(); fp.spawn_eager = AsyncMock(); fp.handles = {}
        tools = [{"name": "mcp__brave-search__b"}]
        calls = {"planner": 0, "completion": 0}

        async def fake(**kw):
            d = kw["defn"]
            if d.agent_type == "planner":
                calls["planner"] += 1
                # わざと 1 個だけ返す
                return SubAgentResult("p", '{"central_question":"CQ","subquestions":["q1"]}', ok=True)
            if d.agent_type == "planner_completion":
                calls["completion"] += 1
                # 補完で 4 個追加
                return SubAgentResult("c", '["q2", "q3", "q4", "q5"]', ok=True)
            if d.agent_type == "searcher":
                return SubAgentResult(kw["briefing"], "f", ok=True)
            if d.agent_type == "cq_refiner":
                return SubAgentResult("r", "中心的問い: CQ2", ok=True)
            if d.agent_type == "magi_decompose":
                return SubAgentResult("d", "[]", ok=True)
            if d.agent_type == "magi_lead":
                return SubAgentResult("l", "STANCE: ok", ok=True)
            if d.agent_type == "integrator":
                return SubAgentResult("i", "**最終**", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _magi_patches(fake, fp, tools):
                es.enter_context(p)
            es.enter_context(patch.object(orch, "DEEP_RESEARCH_MIN_SUBAGENTS", 5))
            es.enter_context(patch.object(orch, "DEEP_RESEARCH_MAX_SUBAGENTS", 6))
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        # planner 1回 + 補完 1回 → 合計 5 サブ質問になる
        assert calls["planner"] == 1
        assert calls["completion"] == 1

    @pytest.mark.asyncio
    async def test_plan_passes_min_subagents_to_builder(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_magi_deps(_approve_by_reaction())
        fp = MagicMock(); fp.spawn_eager = AsyncMock(); fp.handles = {}
        tools = [{"name": "mcp__brave-search__b"}]
        seen = {}

        async def fake(**kw):
            d = kw["defn"]
            if d.agent_type == "planner":
                seen["briefing"] = kw["briefing"]
                # ちょうど5つ返す
                return SubAgentResult("p", '{"central_question":"CQ","subquestions":["q1","q2","q3","q4","q5"]}', ok=True)
            if d.agent_type == "searcher":
                return SubAgentResult(kw["briefing"], "f", ok=True)
            if d.agent_type == "cq_refiner":
                return SubAgentResult("r", "中心的問い: CQ2", ok=True)
            if d.agent_type == "magi_decompose":
                return SubAgentResult("d", "[]", ok=True)
            if d.agent_type == "magi_lead":
                return SubAgentResult("l", "STANCE: ok", ok=True)
            if d.agent_type == "integrator":
                return SubAgentResult("i", "**最終**", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _magi_patches(fake, fp, tools):
                es.enter_context(p)
            es.enter_context(patch.object(orch, "DEEP_RESEARCH_MIN_SUBAGENTS", 5))
            es.enter_context(patch.object(orch, "DEEP_RESEARCH_MAX_SUBAGENTS", 6))
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)
        # planner briefing に最低数の指示が乗ってる
        assert "最低 5 個必須" in seen["briefing"]


class TestMem0InjectionScope:
    """Mem0 長期記憶は clarify/planner/refine/integrator に注入し、MAGI 3視点には渡さない。

    視点の独立性（FR-M2 & ADR-0030）を破らないための分離を回帰テストで固定する。
    """

    def test_mem_prefix_only_when_present(self):
        from services.deep_research.prompts import (
            build_clarify_briefing,
            build_integrator_briefing,
            build_plan_magi_briefing,
            build_refine_cq_briefing,
        )
        block = "[Mem0 — この人についての長期記憶]\n- 先輩は VI 戦略を作成中\n[End of Mem0 memories]"
        for fn, args in [
            (build_clarify_briefing, ("q",)),
            (build_plan_magi_briefing, ("q", 4)),
            (build_refine_cq_briefing, ("q", "digest")),
            (build_integrator_briefing, ("cq", "anon", "digest")),
        ]:
            empty = fn(*args, "")
            with_mem = fn(*args, block)
            assert "<retrieved_memories" not in empty   # 記憶なしでは挿入されない
            assert "<retrieved_memories" in with_mem    # ありなら header 付きで先頭注入
            assert "VI 戦略" in with_mem

    @pytest.mark.asyncio
    async def test_mem0_recall_runs_and_injects_into_4_phases(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_magi_deps(_approve_by_reaction())
        fp, tools = self._pool_tools() if False else (
            MagicMock(spawn_eager=AsyncMock(), handles={}),
            [{"name": "mcp__brave-search__b", "input_schema": {}}],
        )
        mem_block = "[Mem0 — この人についての長期記憶]\n- 先輩は VI 戦略を作成中\n[End of Mem0 memories]"
        briefings = {}

        async def fake_clarify_loop(prompt, channel_id, **kwargs):
            briefings["clarify"] = prompt
            res = MagicMock(); res.text = "- 期間は？"; res.usage_total = None
            return res

        async def fake(**kw):
            d = kw["defn"]; st = kw["state"]; b = kw["briefing"]
            at = d.agent_type
            briefings.setdefault(at, b)
            if at == "planner":
                return SubAgentResult("p", '{"central_question":"CQ","subquestions":["q1"]}', ok=True)
            if at == "searcher":
                st.register_source(url="https://s.com/1", title="t")
                return SubAgentResult(b, "findings", ok=True)
            if at == "cq_refiner":
                return SubAgentResult("r", "中心的問い: CQ2", ok=True)
            if at == "magi_decompose":
                return SubAgentResult("d", '["sub1"]', ok=True)
            if at == "magi_worker":
                return SubAgentResult(b, "メモ [S1]", ok=True)
            if at == "magi_lead":
                return SubAgentResult("l", "STANCE: ok", ok=True)
            if at == "integrator":
                return SubAgentResult("i", "**最終テーゼ** [S1]", ok=True)
            return SubAgentResult("?", "", ok=False)

        async def fake_recall(query, user_id, **kw):
            return [{"memory": "先輩は VI 戦略を作成中"}]

        with ExitStack() as es:
            for p in _magi_patches(fake, fp, tools):
                es.enter_context(p)
            es.enter_context(patch.object(orch, "run_agent_loop", new=fake_clarify_loop))
            # _recall_user_memories 内の遅延 import を本物経路で動かす（search のみ差し替え）
            from services.mem0 import search as mem_search
            es.enter_context(patch.object(mem_search, "search_memories_smart", new=fake_recall))
            await orch.run_deep_research(channel, MagicMock(id=42), "テーマ", deps, skip_clarify=False)

        # 注入される4箇所: clarify / planner / cq_refiner / integrator
        assert "VI 戦略" in briefings["clarify"]
        assert "VI 戦略" in briefings["planner"]
        assert "VI 戦略" in briefings["cq_refiner"]
        assert "VI 戦略" in briefings["integrator"]
        # 視点独立性: MAGI 3視点（分解・ワーカー・lead）には記憶を渡さない
        assert "VI 戦略" not in briefings["magi_decompose"]
        assert "VI 戦略" not in briefings["magi_worker"]
        assert "VI 戦略" not in briefings["magi_lead"]

    def _pool_tools(self):
        fp = MagicMock(); fp.spawn_eager = AsyncMock(); fp.handles = {}
        return fp, [{"name": "mcp__brave-search__b", "input_schema": {}}]


class TestKanaePersona:
    def test_integrator_system_routes_through_main_persona_and_keeps_rigor(self):
        from services.deep_research.prompts import build_integrator_system, INTEGRATOR_SYSTEM
        s = build_integrator_system("先輩")
        assert config.SYSTEM_PROMPT in s    # 本ルートの人格（CLAUDE.md）を土台にする
        assert INTEGRATOR_SYSTEM in s       # その上に integrator 仕様を追加システムで重ねる
        assert "先輩" in s                  # 呼称が反映される
        assert "反証条件" in s              # 分析の厳密さは温存

    def test_integrator_system_unknown_requester_is_polite(self):
        from services.deep_research.prompts import build_integrator_system
        s = build_integrator_system("")
        assert config.SYSTEM_PROMPT in s
        assert "丁寧語" in s                # 呼称不明時は丁寧語、「先輩」とは呼ばない

    def test_integrator_system_enforces_plain_language(self):
        # 出力を「普段の語り口」レベルに噛み砕かせる指示が確実に乗っていること。
        from services.deep_research.prompts import build_integrator_system
        s = build_integrator_system("先輩")
        # 強調側（呼びかけ階層）
        assert "普段の会話と同じ語り口で書く" in s
        # 規律側（言語ルール）
        assert "言語の規律" in s
        assert "1文40字目安" in s          # 文を短く切る指示
        assert "言い換える" in s            # 専門用語を初出で1度だけ言い換え
        # BLUF の構造・厳密さは温存
        assert "テーゼ" in s and "覆る条件" in s

    def test_integrator_does_not_redefine_discord_output_rules(self):
        # Discord 出力ルール（Markdown 記法・見出し・インラインコード等）は本ルートの
        # SYSTEM_PROMPT（CLAUDE.md）が支配する。INTEGRATOR_SYSTEM 側で再定義していると
        # 競合・劣化するので、ここでは骨格しか書かない。
        from services.deep_research.prompts import INTEGRATOR_SYSTEM
        # 旧版にあった独自の Discord 記法ルールが消えていること（再定義禁止の回帰）
        assert "Discord markdown" not in INTEGRATOR_SYSTEM
        assert "テーブル・水平線は使わない" not in INTEGRATOR_SYSTEM
        # 代わりに「システムプロンプトの Discord 出力ルールに従う」明示があること
        assert "Discord 出力ルール" in INTEGRATOR_SYSTEM
        assert "再定義しない" in INTEGRATOR_SYSTEM
        # SYSTEM_PROMPT には本物の Discord 出力ルールが入っていることを確認（本ルート土台）
        assert "Discord出力ルール" in config.SYSTEM_PROMPT
        assert "インラインコード使用ルール" in config.SYSTEM_PROMPT
        assert "見出し使用ルール" in config.SYSTEM_PROMPT

    def test_kanae_honorific_resolves_from_env_map(self):
        with patch.dict(config.SPEAKER_LABELS, {1234: "haroin"}, clear=False), \
             patch.dict(config.KANAE_HONORIFICS, {"haroin": "先輩"}, clear=False):
            assert config.kanae_honorific(1234) == "先輩"
            assert config.kanae_honorific(99999) == ""    # 未知の相手 → 丁寧語

    @pytest.mark.asyncio
    async def test_integrator_uses_persona_system_for_requester(self):
        # run_integrator が呼称解決済みの人格システムを integrator subagent に渡す。
        captured = {}

        async def fake(**kw):
            captured["system"] = kw["defn"].system_prompt
            return SubAgentResult("i", "**テーゼ** 結論 [S1]", ok=True)

        state = ResearchState(query="q", clarified_query="CQ")
        state.register_source(url="https://a.com/1", title="A")
        state.lens_outputs = [LensResult("science", "科学", "S", "本文 [S1]", ok=True)]
        ctx = SimpleNamespace(
            api_client=MagicMock(), pool=MagicMock(), server_locks={},
            tools_for_api=[], state=state, thread=SimpleNamespace(id=1),
            user=SimpleNamespace(id=1234),
        )
        with patch.object(magi, "run_subagent", new=fake), \
             patch.dict(config.SPEAKER_LABELS, {1234: "haroin"}, clear=False), \
             patch.dict(config.KANAE_HONORIFICS, {"haroin": "先輩"}, clear=False):
            out = await magi.run_integrator(ctx)
        assert config.SYSTEM_PROMPT in captured["system"]   # 本ルートの人格を通す
        assert "先輩" in captured["system"]
        assert out is not None and "テーゼ" in out


def _magi_ctx(tools, state):
    return SimpleNamespace(
        api_client=MagicMock(), pool=MagicMock(), server_locks={},
        tools_for_api=tools, state=state, thread=SimpleNamespace(id=777),
        anchor=None, counter={}, tool_cb=None, user=None, deps=None,
    )


class _FakeView:
    def __init__(self):
        self.tools = 0

    async def waiting(self): ...
    async def working(self, note="作業中…"): ...
    async def on_tool(self, name, tool_input): self.tools += 1
    async def finish(self, ok, gist=""): ...


class TestMagiLensNested:
    @pytest.mark.asyncio
    async def test_decompose_then_workers_then_lead(self):
        order = []
        models = {}
        efforts = {}

        async def fake(**kw):
            d = kw["defn"]
            order.append(d.agent_type)
            models[d.agent_type] = d.model
            efforts[d.agent_type] = d.effort
            if d.agent_type == "magi_decompose":
                return SubAgentResult("d", '["sub1", "sub2"]', ok=True)
            if d.agent_type == "magi_worker":
                return SubAgentResult(kw["briefing"], "メモ [S1]", ok=True)
            if d.agent_type == "magi_lead":
                return SubAgentResult("l", "STANCE: 私の立場\n【手堅い結論】\n- x [S1]", ok=True)
            return SubAgentResult("?", "", ok=False)

        state = ResearchState(query="q", central_question="CQ")
        ctx = _magi_ctx([{"name": "mcp__brave-search__b"}], state)
        view = _FakeView()
        sema = asyncio.Semaphore(2)
        with patch.object(magi, "run_subagent", new=fake), \
             patch.object(magi, "DEEP_RESEARCH_MAGI_MAX_WORKERS", 2):
            res = await magi.run_magi_lens(ctx, "science", view, sema)

        # 入れ子の順序: 分解 → ワーカー×2 → lead
        assert order[0] == "magi_decompose"
        assert order.count("magi_worker") == 2
        assert order[-1] == "magi_lead"
        # 安いモデル(Sonnet)＝分解/ワーカー、Opus(effort=max)＝lead
        assert efforts["magi_decompose"] is None
        assert efforts["magi_worker"] is None
        assert efforts["magi_lead"] == magi.DEEP_RESEARCH_MAGI_EFFORT
        assert models["magi_worker"] == magi.DEEP_RESEARCH_MAGI_WORKER_MODEL
        assert models["magi_lead"] == magi.DEEP_RESEARCH_MAGI_MODEL
        # STANCE がパースされる
        assert res.ok and res.stance == "私の立場"

    @pytest.mark.asyncio
    async def test_lead_failure_returns_not_ok(self):
        async def fake(**kw):
            d = kw["defn"]
            if d.agent_type == "magi_decompose":
                return SubAgentResult("d", "[]", ok=True)    # no subtasks
            if d.agent_type == "magi_lead":
                return SubAgentResult("l", "", ok=False, error="429")
            return SubAgentResult("?", "", ok=False)

        ctx = _magi_ctx([], ResearchState(query="q", central_question="CQ"))
        with patch.object(magi, "run_subagent", new=fake):
            res = await magi.run_magi_lens(ctx, "skeptic", _FakeView(), asyncio.Semaphore(2))
        assert res.ok is False
        assert res.lens_key == "skeptic"

    @pytest.mark.asyncio
    async def test_shared_semaphore_bounds_worker_concurrency(self):
        # 並行性アサーション: 共有 sema(2) がワーカー同時実行を MAX_PARALLEL 以下に律速する。
        live = {"now": 0, "peak": 0}

        async def fake(**kw):
            d = kw["defn"]
            if d.agent_type == "magi_decompose":
                return SubAgentResult("d", '["a", "b", "c", "d"]', ok=True)  # 4 workers
            if d.agent_type == "magi_worker":
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
                await asyncio.sleep(0.02)        # 重なりを観測できるよう少し滞留
                live["now"] -= 1
                return SubAgentResult(kw["briefing"], "メモ", ok=True)
            if d.agent_type == "magi_lead":
                return SubAgentResult("l", "STANCE: ok", ok=True)
            return SubAgentResult("?", "", ok=False)

        ctx = _magi_ctx([{"name": "mcp__brave-search__b"}],
                        ResearchState(query="q", central_question="CQ"))
        with patch.object(magi, "run_subagent", new=fake), \
             patch.object(magi, "DEEP_RESEARCH_MAGI_MAX_WORKERS", 4):
            await magi.run_magi_lens(ctx, "science", _FakeView(), asyncio.Semaphore(2))
        assert live["peak"] <= 2                  # sema(2) を超えて並走しない
        assert live["peak"] >= 2                  # かつ実際に並列化されている


# --- フル MAGI パイプライン（orchestrator 経由）---
def _make_magi_deps(wait_for, bot_id=999000):
    bot = MagicMock()
    bot.wait_for = wait_for
    bot.user = SimpleNamespace(id=bot_id)
    return orch.DeepResearchDeps(
        bot=bot, send_segmented=AsyncMock(),
        get_http_session=AsyncMock(), track_task=MagicMock(),
    )


def _approve_by_reaction():
    async def _wf(event, *, check=None, timeout=None):
        if event == "raw_reaction_add":
            return SimpleNamespace(message_id=1, user_id=1)
        await asyncio.Event().wait()   # message: never arrives
    return _wf


def _magi_patches(fake, fake_pool, tools, extra=None):
    base = [
        patch.object(orch, "DEEP_RESEARCH_MULTI_AGENT", True),
        patch.object(orch, "DEEP_RESEARCH_MAGI_ENABLED", True),
        patch.object(orch, "DEEP_RESEARCH_MAX_PARALLEL", 2),
        patch.object(orch, "DEEP_RESEARCH_MAX_SUBAGENTS", 4),
        patch.object(orch, "MCP_SERVERS_WEB", {"brave-search": {"x": 1}}),
        patch.object(orch, "make_api_client", new=AsyncMock(return_value=MagicMock())),
        patch.object(orch, "McpPool", return_value=fake_pool),
        patch.object(orch, "mcp_tools_to_anthropic", return_value=tools),
        patch.object(orch, "run_subagent", new=fake),
        patch.object(magi, "run_subagent", new=fake),
        patch.object(magi, "DEEP_RESEARCH_MAGI_MAX_WORKERS", 2),
    ]
    return base + (extra or [])


class TestMagiPipeline:
    def _pool_tools(self):
        fp = MagicMock(); fp.spawn_eager = AsyncMock(); fp.handles = {}
        return fp, [{"name": "mcp__brave-search__b", "input_schema": {}}]

    @pytest.mark.asyncio
    async def test_full_pipeline_cost_structure(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_magi_deps(_approve_by_reaction())
        fp, tools = self._pool_tools()
        calls = {}
        captured = {}

        async def fake(**kw):
            d = kw["defn"]; st = kw["state"]
            at = d.agent_type
            calls[at] = calls.get(at, 0) + 1
            if at == "planner":
                return SubAgentResult("p", '{"central_question":"暫定CQ","subquestions":["q1","q2"]}', ok=True)
            if at == "searcher":
                st.register_source(url=f"https://s.com/{calls['searcher']}", title="t")
                return SubAgentResult(kw["briefing"], "findings", ok=True)
            if at == "cq_refiner":
                return SubAgentResult("r", "中心的問い: 研いだCQ\n核心: 緊張に根ざす", ok=True)
            if at == "magi_decompose":
                return SubAgentResult("d", '["sub1", "sub2"]', ok=True)
            if at == "magi_worker":
                return SubAgentResult(kw["briefing"], "分析メモ [S1]", ok=True)
            if at == "magi_lead":
                return SubAgentResult("l", "STANCE: 立場\n【手堅い結論】\n- x [S1]", ok=True)
            if at == "integrator":
                captured["briefing"] = kw["briefing"]
                captured["effort"] = d.effort
                return SubAgentResult("i", "**テーゼ** 結論は真 [S1]", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _magi_patches(fake, fp, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        # コスト構造（PoC）: Opus は planner+refine+3 lead+integrator=6、検索/部品は Sonnet。
        assert calls["planner"] == 1
        assert calls["searcher"] == 2
        assert calls["cq_refiner"] == 1
        assert calls["magi_decompose"] == 3        # 1 per lens
        assert calls["magi_worker"] == 6           # 2 subtasks × 3 lenses
        assert calls["magi_lead"] == 3
        assert calls["integrator"] == 1
        # integrator は effort=max、入力は匿名化（視点A/B/C・神名は伏せ）
        assert captured["effort"] == config.DEEP_RESEARCH_MAGI_EFFORT
        assert "視点A" in captured["briefing"]
        assert "メルキオール" not in captured["briefing"]
        # BLUF レポートが配信され、機械ソースが付与される
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "テーゼ" in sent_text and "情報ソース" in sent_text

    @pytest.mark.asyncio
    async def test_lens_reasoning_not_posted_to_thread(self):
        # 詳細な推論ログは出さない（隠す）。STANCE 本文等が thread に投稿されないこと。
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_magi_deps(_approve_by_reaction())
        fp, tools = self._pool_tools()
        lead_body = "STANCE: 私の立場\n【手堅い結論】\n- 主張 [S1] ｜ 反証条件: X が観測されれば覆る"
        stream_cbs = []

        async def fake(**kw):
            d = kw["defn"]; st = kw["state"]
            at = d.agent_type
            if at == "planner":
                return SubAgentResult("p", '{"central_question":"CQ","subquestions":["q1"]}', ok=True)
            if at == "searcher":
                st.register_source(url="https://s.com/1", title="t")
                return SubAgentResult(kw["briefing"], "f", ok=True)
            if at == "cq_refiner":
                return SubAgentResult("r", "中心的問い: CQ2", ok=True)
            if at == "magi_decompose":
                return SubAgentResult("d", "[]", ok=True)
            if at == "magi_lead":
                stream_cbs.append(kw.get("text_stream_callback"))
                return SubAgentResult("l", lead_body, ok=True)
            if at == "integrator":
                return SubAgentResult("i", "**最終テーゼ**", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _magi_patches(fake, fp, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        texts = [str(c.args[0]) for c in thread.send.call_args_list]
        # レンズの推論全文（"の推論" 見出しや STANCE 本文）は thread に出さない
        assert not any("の推論" in t for t in texts)
        assert not any("私の立場" in t for t in texts)
        # lead にライブ stream callback も渡さない（詳細ログ抑制）
        assert all(cb is None for cb in stream_cbs)
        # 進捗の1行ステータス（待機中→完了）は残る
        assert any("待機中" in t for t in texts)
        # 最終 BLUF レポートは従来どおり配信される
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "最終テーゼ" in sent_text

    @pytest.mark.asyncio
    async def test_one_lens_fails_integrator_still_runs(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_magi_deps(_approve_by_reaction())
        fp, tools = self._pool_tools()
        integ = {"ran": False}

        async def fake(**kw):
            d = kw["defn"]; st = kw["state"]
            at = d.agent_type
            if at == "planner":
                return SubAgentResult("p", '{"central_question":"CQ","subquestions":["q1"]}', ok=True)
            if at == "searcher":
                st.register_source(url="https://s.com/1", title="t")
                return SubAgentResult(kw["briefing"], "f", ok=True)
            if at == "cq_refiner":
                return SubAgentResult("r", "中心的問い: CQ2", ok=True)
            if at == "magi_decompose":
                return SubAgentResult("d", "[]", ok=True)
            if at == "magi_lead":
                # intuition lead だけ失敗
                if "バルタザール" in d.system_prompt:
                    raise RuntimeError("intuition boom")
                return SubAgentResult("l", "STANCE: ok", ok=True)
            if at == "integrator":
                integ["ran"] = True
                return SubAgentResult("i", "**統合テーゼ**", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _magi_patches(fake, fp, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        assert integ["ran"] is True             # 1レンズ失敗でも統合は走る
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "統合テーゼ" in sent_text

    @pytest.mark.asyncio
    async def test_all_lenses_fail_degrades_to_raw(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        deps = _make_magi_deps(_approve_by_reaction())
        fp, tools = self._pool_tools()
        integ = {"ran": False}

        async def fake(**kw):
            d = kw["defn"]; st = kw["state"]
            at = d.agent_type
            if at == "planner":
                return SubAgentResult("p", '{"central_question":"CQ","subquestions":["q1"]}', ok=True)
            if at == "searcher":
                st.register_source(url="https://s.com/1", title="t")
                return SubAgentResult(kw["briefing"], "findings ok", ok=True)
            if at == "cq_refiner":
                return SubAgentResult("r", "中心的問い: CQ2", ok=True)
            if at == "magi_decompose":
                return SubAgentResult("d", "[]", ok=True)
            if at == "magi_lead":
                return SubAgentResult("l", "", ok=False, error="all dead")
            if at == "integrator":
                integ["ran"] = True
                return SubAgentResult("i", "should not run", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _magi_patches(fake, fp, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        assert integ["ran"] is False            # 全滅 → integrator を呼ばず degrade
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "簡易版" in sent_text             # 事実統合フォールバック

    @pytest.mark.asyncio
    async def test_gate_reply_corrects_then_approves(self):
        from contextlib import ExitStack
        channel, origin, thread, anchor = _make_channel()
        fp, tools = self._pool_tools()

        rounds = {"n": 0}

        async def _wf(event, *, check=None, timeout=None):
            # round0: リプライ訂正 → round1: リアクション承認
            if event == "message":
                if rounds["n"] == 0:
                    rounds["n"] = 1
                    return SimpleNamespace(
                        content="もっと因果に寄せて",
                        mentions=[],
                        channel=SimpleNamespace(id=thread.id),
                        author=SimpleNamespace(id=1, bot=False),
                    )
                await asyncio.Event().wait()
            if event == "raw_reaction_add":
                if rounds["n"] >= 1:
                    return SimpleNamespace(message_id=1, user_id=1)
                await asyncio.Event().wait()
            await asyncio.Event().wait()

        deps = _make_magi_deps(_wf)
        refines = {"n": 0}

        async def fake(**kw):
            d = kw["defn"]; st = kw["state"]
            at = d.agent_type
            if at == "planner":
                return SubAgentResult("p", '{"central_question":"CQ","subquestions":["q1"]}', ok=True)
            if at == "searcher":
                st.register_source(url="https://s.com/1", title="t")
                return SubAgentResult(kw["briefing"], "f", ok=True)
            if at == "cq_refiner":
                refines["n"] += 1
                return SubAgentResult("r", f"中心的問い: CQ-{refines['n']}", ok=True)
            if at == "magi_decompose":
                return SubAgentResult("d", "[]", ok=True)
            if at == "magi_lead":
                return SubAgentResult("l", "STANCE: ok", ok=True)
            if at == "integrator":
                return SubAgentResult("i", "**最終**", ok=True)
            return SubAgentResult("?", "", ok=False)

        with ExitStack() as es:
            for p in _magi_patches(fake, fp, tools):
                es.enter_context(p)
            await orch.run_deep_research(channel, MagicMock(id=1), "テーマ", deps, skip_clarify=True)

        # Stage2 refine（1回）+ 訂正後の再研磨（1回）= 2
        assert refines["n"] == 2
        _a, sent_text, _f = deps.send_segmented.call_args.args
        assert "最終" in sent_text


class TestMagiHelpers:
    def _ctx(self, state, *, deps=None, user=None, thread=None):
        return SimpleNamespace(
            api_client=MagicMock(), pool=MagicMock(), server_locks={},
            tools_for_api=[], state=state, thread=thread or SimpleNamespace(id=1),
            anchor=None, counter={}, tool_cb=None, user=user, deps=deps,
        )

    @pytest.mark.asyncio
    async def test_gate_noop_without_bot_or_user(self):
        # deps/user 無し → 承認待ちせず即 return（CI/headless で詰まらない）
        ctx = self._ctx(ResearchState(query="q", central_question="CQ"), deps=None, user=None)
        await orch._central_question_gate(ctx)   # should not raise / not hang

    @pytest.mark.asyncio
    async def test_await_gate_reply_with_auto_mention_is_correct_not_approve(self):
        # 回帰: Discord「返信」は元発言者(=bot)を自動メンションする。reference 付きは
        # 純粋な @ メンションでなくリプライ＝訂正として扱う（approve に化けない）。
        thread = SimpleNamespace(id=42)
        bot_user = SimpleNamespace(id=999)
        reply = SimpleNamespace(
            content="ROI でなく機会損失で再フレーミングしてほしい",
            mentions=[bot_user],   # Reply による auto-mention
            reference=SimpleNamespace(message_id=1),
            channel=SimpleNamespace(id=42),
            author=SimpleNamespace(id=7, bot=False),
        )

        async def _wf(event, *, check=None, timeout=None):
            if event == "message":
                return reply
            await asyncio.Event().wait()
        bot = MagicMock(); bot.wait_for = _wf; bot.user = bot_user
        ctx = self._ctx(ResearchState(query="q"), deps=SimpleNamespace(bot=bot),
                        user=SimpleNamespace(id=7), thread=thread)
        decision, text = await orch._await_gate_response(ctx, SimpleNamespace(id=1))
        assert decision == "correct"
        assert "機会損失" in text

    @pytest.mark.asyncio
    async def test_await_gate_pure_mention_without_reply_approves(self):
        # 純粋な @ メンション（reference 無し）は従来通り approve。
        thread = SimpleNamespace(id=42)
        bot_user = SimpleNamespace(id=999)
        msg = SimpleNamespace(
            content="@bot OKそのまま進めて", mentions=[bot_user], reference=None,
            channel=SimpleNamespace(id=42), author=SimpleNamespace(id=7, bot=False),
        )

        async def _wf(event, *, check=None, timeout=None):
            if event == "message":
                return msg
            await asyncio.Event().wait()
        bot = MagicMock(); bot.wait_for = _wf; bot.user = bot_user
        ctx = self._ctx(ResearchState(query="q"), deps=SimpleNamespace(bot=bot),
                        user=SimpleNamespace(id=7), thread=thread)
        decision, _ = await orch._await_gate_response(ctx, SimpleNamespace(id=1))
        assert decision == "approve"

    @pytest.mark.asyncio
    async def test_await_gate_mention_approves(self):
        thread = SimpleNamespace(id=42)
        msg = SimpleNamespace(
            content="@bot いいね", mentions=[SimpleNamespace(id=999)],
            channel=SimpleNamespace(id=42), author=SimpleNamespace(id=7, bot=False),
        )

        async def _wf(event, *, check=None, timeout=None):
            if event == "message":
                assert check(msg) is True       # _mcheck クロージャを実際に通す
                return msg
            await asyncio.Event().wait()

        bot = MagicMock(); bot.wait_for = _wf; bot.user = SimpleNamespace(id=999)
        ctx = self._ctx(ResearchState(query="q"), deps=SimpleNamespace(bot=bot),
                        user=SimpleNamespace(id=7), thread=thread)
        decision, text = await orch._await_gate_response(ctx, SimpleNamespace(id=1))
        assert decision == "approve"

    @pytest.mark.asyncio
    async def test_gate_wait_exception_proceeds(self):
        bot = MagicMock(); bot.wait_for = AsyncMock(); bot.user = SimpleNamespace(id=9)
        thread = MagicMock(); thread.id = 5
        thread.send = AsyncMock(return_value=SimpleNamespace(id=1))
        ctx = self._ctx(ResearchState(query="q", central_question="CQ"),
                        deps=SimpleNamespace(bot=bot), user=SimpleNamespace(id=7), thread=thread)
        with patch.object(orch, "_await_gate_response", side_effect=RuntimeError("blew up")):
            await orch._central_question_gate(ctx)   # 例外は握って続行（詰まらせない）

    @pytest.mark.asyncio
    async def test_parse_refined_cq_fallback_first_line(self):
        cq, rationale = orch._parse_refined_cq("ラベル無しの問い本文\n二行目")
        assert cq == "ラベル無しの問い本文"
        assert rationale == ""

    @pytest.mark.asyncio
    async def test_present_cq_returns_none_on_send_failure(self):
        import discord
        thread = MagicMock()
        thread.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
        ctx = self._ctx(ResearchState(query="q", central_question="CQ"), thread=thread)
        assert await orch._present_cq(ctx) is None

    @pytest.mark.asyncio
    async def test_gate_returns_when_cannot_present(self):
        import discord
        thread = MagicMock()
        thread.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
        bot = MagicMock(); bot.wait_for = AsyncMock(); bot.user = SimpleNamespace(id=9)
        ctx = self._ctx(ResearchState(query="q", central_question="CQ"),
                        deps=SimpleNamespace(bot=bot), user=SimpleNamespace(id=7), thread=thread)
        await orch._central_question_gate(ctx)   # present None → return（無限ループしない）
        bot.wait_for.assert_not_called()

    @pytest.mark.asyncio
    async def test_refine_falls_back_to_clarified_query(self):
        async def fake(**kw):
            return SubAgentResult("r", "", ok=False, error="refiner down")
        state = ResearchState(query="q", clarified_query="元テーマ")
        ctx = self._ctx(state)
        with patch.object(orch, "run_subagent", new=fake):
            await orch._refine_central_question(ctx)
        assert state.central_question == "元テーマ"   # 失敗時はテーマを問いに

    @pytest.mark.asyncio
    async def test_refine_extracts_next_line_question_not_label(self):
        # 実バグ回帰: refiner が次行形式で返しても、中心的問いがラベルで埋まらない。
        async def fake(**kw):
            return SubAgentResult(
                "r", "中心的問い:\nAIブームはバブルか？\n核心:\n過熱の緊張", ok=True,
            )
        state = ResearchState(query="q", clarified_query="AIバブル調査")
        state.central_question = "暫定の問い"   # planner 暫定
        ctx = self._ctx(state)
        with patch.object(orch, "run_subagent", new=fake):
            await orch._refine_central_question(ctx)
        assert state.central_question == "AIブームはバブルか？"
        assert state.central_question != "中心的問い:"

    @pytest.mark.asyncio
    async def test_refine_label_only_output_falls_back(self):
        # refiner がラベルしか返さない病的ケース → clarified_query にフォールバックし、
        # 「中心的問い:」のような空欄を lead に渡さない。
        async def fake(**kw):
            return SubAgentResult("r", "中心的問い:\n核心:", ok=True)
        state = ResearchState(query="q", clarified_query="AIバブル調査")
        ctx = self._ctx(state)
        with patch.object(orch, "run_subagent", new=fake):
            await orch._refine_central_question(ctx)
        assert state.central_question == "AIバブル調査"
        assert orch._meaningful_cq(state.central_question) is True

    @pytest.mark.asyncio
    async def test_lens_worker_exception_skipped_lead_still_runs(self):
        async def fake(**kw):
            d = kw["defn"]
            if d.agent_type == "magi_decompose":
                return SubAgentResult("d", '["s1", "s2"]', ok=True)
            if d.agent_type == "magi_worker":
                if "s2" in kw["briefing"]:
                    raise RuntimeError("worker boom")    # gather(return_exceptions) で握る
                return SubAgentResult(kw["briefing"], "メモ [S1]", ok=True)
            if d.agent_type == "magi_lead":
                return SubAgentResult("l", "STANCE: 生存", ok=True)
            return SubAgentResult("?", "", ok=False)

        ctx = SimpleNamespace(
            api_client=MagicMock(), pool=MagicMock(), server_locks={},
            tools_for_api=[{"name": "mcp__brave-search__b"}],
            state=ResearchState(query="q", central_question="CQ"),
            thread=SimpleNamespace(id=1),
        )
        with patch.object(magi, "run_subagent", new=fake), \
             patch.object(magi, "DEEP_RESEARCH_MAGI_MAX_WORKERS", 2):
            res = await magi.run_magi_lens(ctx, "science", _FakeView(), asyncio.Semaphore(2))
        assert res.ok and res.stance == "生存"   # 1ワーカー死亡でも lead は完走


# ---------------------------------------------------------------------------
# 中心的問いゲート — 訂正回数上限（_research_busy の占有 DoS 対策）
# ---------------------------------------------------------------------------

class TestCqCorrectionCap:
    @pytest.mark.asyncio
    async def test_gate_auto_proceeds_after_max_corrections(self):
        state = ResearchState(query="q", central_question="CQ")
        ctx = SimpleNamespace(
            deps=SimpleNamespace(bot=MagicMock()),
            user=SimpleNamespace(id=1),
            state=state,
            thread=SimpleNamespace(id=1),
        )
        refine = AsyncMock()
        with patch.object(orch, "_present_cq", new=AsyncMock(return_value=SimpleNamespace(id=9))), \
             patch.object(orch, "_await_gate_response",
                          new=AsyncMock(return_value=("correct", "もっと具体的に"))), \
             patch.object(orch, "_refine_central_question", new=refine), \
             patch.object(orch, "_safe_send", new=AsyncMock()), \
             patch.object(orch, "DEEP_RESEARCH_MAX_CQ_CORRECTIONS", 2):
            # 訂正しか返ってこなくても while が有限で終わること
            await asyncio.wait_for(orch._central_question_gate(ctx), timeout=5)
        assert refine.await_count == 2  # cap 回だけ研ぎ直して自動続行
        assert state.central_question.count("ユーザーの訂正指示") == 2

    @pytest.mark.asyncio
    async def test_gate_approve_short_circuits(self):
        state = ResearchState(query="q", central_question="CQ")
        ctx = SimpleNamespace(
            deps=SimpleNamespace(bot=MagicMock()),
            user=SimpleNamespace(id=1),
            state=state,
            thread=SimpleNamespace(id=1),
        )
        refine = AsyncMock()
        with patch.object(orch, "_present_cq", new=AsyncMock(return_value=SimpleNamespace(id=9))), \
             patch.object(orch, "_await_gate_response",
                          new=AsyncMock(return_value=("approve", ""))), \
             patch.object(orch, "_refine_central_question", new=refine), \
             patch.object(orch, "_safe_send", new=AsyncMock()):
            await asyncio.wait_for(orch._central_question_gate(ctx), timeout=5)
        assert refine.await_count == 0


# ---------------------------------------------------------------------------
# evidence digest — ソース一覧の上限
# ---------------------------------------------------------------------------

class TestDigestSourceCap:
    def test_source_list_is_capped(self):
        from services.deep_research import prompts as prompts_mod
        from services.deep_research.types import SourceRef
        state = ResearchState(query="q")
        for i in range(1, prompts_mod._DIGEST_MAX_SOURCES + 6):
            sid = f"S{i}"
            state.sources[f"u{i}"] = SourceRef(
                sid=sid, title=f"t{i}", url=f"https://example.com/{i}",
            )
        state.results.append(SubAgentResult(subq="q1", summary_md="要約テキスト"))
        digest = prompts_mod.build_evidence_digest(state)
        assert "[S1]" in digest
        assert f"[S{prompts_mod._DIGEST_MAX_SOURCES}]" in digest
        assert f"[S{prompts_mod._DIGEST_MAX_SOURCES + 1}]" not in digest
        assert "他 5 件のソースを省略" in digest
        # fixed 部が肥大しても searcher 要約が落ちないこと
        assert "要約テキスト" in digest


# ---------------------------------------------------------------------------
# report critic（最終レポートの adversarial verification）
# ---------------------------------------------------------------------------

class TestReportCritic:
    def _ctx(self):
        return SimpleNamespace(
            api_client=MagicMock(), pool=MagicMock(), server_locks={},
            state=ResearchState(query="q", central_question="CQ"),
            thread=SimpleNamespace(id=1),
        )

    def _integ_def(self):
        return ResearchAgentDef(
            agent_type="integrator", model="m", system_prompt="s",
            max_tool_turns=1, idle_timeout_s=30,
        )

    @pytest.mark.asyncio
    async def test_pass_keeps_original(self):
        calls = []

        async def fake(**kw):
            calls.append(kw["defn"].agent_type)
            return SubAgentResult("c", "PASS", ok=True)

        with patch.object(magi, "run_subagent", new=fake):
            out = await magi._critic_pass(
                self._ctx(), self._integ_def(), "CQ", "元レポート", "DIGEST",
            )
        assert out == "元レポート"
        assert calls == ["report_critic"]  # 修正 integrator は走らない

    @pytest.mark.asyncio
    async def test_findings_trigger_single_revision(self):
        calls = []

        async def fake(**kw):
            calls.append(kw["defn"].agent_type)
            if kw["defn"].agent_type == "report_critic":
                return SubAgentResult("c", "1. [S3] の引用が本文と食い違う", ok=True)
            return SubAgentResult("i", "修正版レポート", ok=True)

        with patch.object(magi, "run_subagent", new=fake):
            out = await magi._critic_pass(
                self._ctx(), self._integ_def(), "CQ", "元レポート", "DIGEST",
            )
        assert out == "修正版レポート"
        assert calls == ["report_critic", "integrator"]

    @pytest.mark.asyncio
    async def test_revision_failure_keeps_original(self):
        async def fake(**kw):
            if kw["defn"].agent_type == "report_critic":
                return SubAgentResult("c", "指摘あり", ok=True)
            return SubAgentResult("i", "", ok=False, error="500")

        with patch.object(magi, "run_subagent", new=fake):
            out = await magi._critic_pass(
                self._ctx(), self._integ_def(), "CQ", "元レポート", "DIGEST",
            )
        assert out == "元レポート"

    @pytest.mark.asyncio
    async def test_critic_exception_keeps_original(self):
        with patch.object(magi, "run_subagent",
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            out = await magi._critic_pass(
                self._ctx(), self._integ_def(), "CQ", "元レポート", "DIGEST",
            )
        assert out == "元レポート"

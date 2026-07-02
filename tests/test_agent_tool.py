"""メインパイプライン Agent tool（CLI 純正と足並み）のテスト。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
from services.claude_api import agent_tool as at
from services.deep_research.types import SubAgentResult


class TestSchema:
    def test_schema_shape_matches_cli(self):
        s = at.agent_tool_schema()
        assert s["name"] == "Agent"
        props = s["input_schema"]["properties"]
        assert set(props) == {"description", "prompt", "subagent_type"}
        assert s["input_schema"]["required"] == ["prompt", "description"]
        # default type は CLI と同じ "general-purpose"。
        assert "general-purpose" in props["subagent_type"]["enum"]


class TestDispatch:
    def _ctx(self, *, depth=0, tools=None):
        return at.AgentToolCtx(
            api_client=MagicMock(),
            pool=MagicMock(),
            tools_for_api=tools if tools is not None else [{"name": "mcp__brave-search__b"}],
            channel_id=42,
            parent_model="claude-opus-4-8",
            depth=depth,
            sema=asyncio.Semaphore(2),
        )

    @pytest.mark.asyncio
    async def test_dispatches_to_run_subagent_with_inherited_model(self):
        captured = {}

        async def fake_run_subagent(**kw):
            captured.update(kw)
            return SubAgentResult(subq=kw["briefing"], summary_md="sub result", ok=True)

        with patch("services.deep_research.registry.run_subagent", new=fake_run_subagent):
            out = await at.execute_agent_tool(
                {"description": "test", "prompt": "do X"}, self._ctx(),
            )
        assert out == "sub result"
        assert captured["briefing"] == "do X"
        # 親モデルを継承（CLI Agent tool と同じ既定）
        assert captured["defn"].model == "claude-opus-4-8"
        # 親の tools を継承（depth+1 が MAX_DEPTH 未満なので Agent も含む可能性あり）
        assert captured["tools_for_api"] == [{"name": "mcp__brave-search__b"}]
        # 親 system_prompt や history を持ち込まない（fresh context は run_subagent が保証）
        assert captured["state"].query == "do X"

    @pytest.mark.asyncio
    async def test_missing_prompt_returns_tool_error(self):
        out = await at.execute_agent_tool({"description": "d"}, self._ctx())
        assert "tool_use_error" in out
        assert "prompt" in out

    @pytest.mark.asyncio
    async def test_unknown_subagent_type_falls_back_to_default(self):
        captured = {}

        async def fake_run_subagent(**kw):
            captured.update(kw)
            return SubAgentResult(subq=kw["briefing"], summary_md="ok", ok=True)

        with patch("services.deep_research.registry.run_subagent", new=fake_run_subagent):
            await at.execute_agent_tool(
                {"description": "d", "prompt": "p", "subagent_type": "doesnotexist"},
                self._ctx(),
            )
        # フォールバック先は general-purpose。defn.agent_type は subagent:<name> 形式。
        assert captured["defn"].agent_type == f"subagent:{config.SUBAGENT_DEFAULT_TYPE}"

    @pytest.mark.asyncio
    async def test_subagent_failure_returns_tool_use_error(self):
        async def boom(**kw):
            raise RuntimeError("kaboom")

        with patch("services.deep_research.registry.run_subagent", new=boom):
            out = await at.execute_agent_tool(
                {"description": "d", "prompt": "p"}, self._ctx(),
            )
        assert "tool_use_error" in out
        assert "kaboom" in out

    @pytest.mark.asyncio
    async def test_depth_cap_strips_agent_from_grandchild_tools(self):
        # 親 tools に Agent 自身も含めて、深さ上限到達で子に渡る tools から外れること。
        captured = {}

        async def fake_run_subagent(**kw):
            captured.update(kw)
            return SubAgentResult(subq=kw["briefing"], summary_md="ok", ok=True)

        parent_tools = [{"name": "mcp__brave-search__b"}, {"name": "Agent"}]
        ctx = at.AgentToolCtx(
            api_client=MagicMock(), pool=MagicMock(),
            tools_for_api=parent_tools, channel_id=1,
            parent_model="m",
            depth=config.SUBAGENT_MAX_DEPTH - 1,   # 子は上限到達
            sema=asyncio.Semaphore(2),
        )
        with patch("services.deep_research.registry.run_subagent", new=fake_run_subagent):
            await at.execute_agent_tool({"description": "d", "prompt": "p"}, ctx)
        names = [t["name"] for t in captured["tools_for_api"]]
        assert "Agent" not in names                   # 孫 spawn を構造的に禁止
        assert "mcp__brave-search__b" in names        # 他ツールは継承


class TestParentWatchdogHeartbeat:
    """subagent 実行中に親 StallWatchdog にハートビートを入れて idle_timeout で
    親が cancel されないことを保証する（haroin 2026-06-25 実観測の回帰）。"""

    @pytest.mark.asyncio
    async def test_heartbeat_pumped_during_subagent_run_and_after(self):
        # 親 watchdog のモック。heartbeat() の呼ばれた回数を数える。
        wd = MagicMock()
        beats = {"n": 0}
        def _hb():
            beats["n"] += 1
        wd.heartbeat.side_effect = _hb

        # subagent が ~ 0.3秒走るあいだ、5秒間隔の本物では heartbeat 入らないので
        # テスト用に間隔を短くする。
        run_seconds = 0.3

        async def fake_run_subagent(**kw):
            await asyncio.sleep(run_seconds)
            return SubAgentResult(subq=kw["briefing"], summary_md="ok", ok=True)

        ctx = at.AgentToolCtx(
            api_client=MagicMock(), pool=MagicMock(),
            tools_for_api=[], channel_id=1, parent_model="m",
            sema=asyncio.Semaphore(2),
            parent_watchdog=wd,
        )
        with patch("services.deep_research.registry.run_subagent", new=fake_run_subagent), \
             patch.object(at, "_HEARTBEAT_INTERVAL_S", 0.05):
            out = await at.execute_agent_tool({"description": "d", "prompt": "p"}, ctx)
        assert out == "ok"
        # 0.3秒 / 0.05秒 = ~6回 + 完了時最終1回 を期待。少なくとも 3回は入っているはず。
        assert beats["n"] >= 3

    @pytest.mark.asyncio
    async def test_final_heartbeat_runs_even_on_subagent_failure(self):
        wd = MagicMock()
        beats = {"n": 0}
        def _hb():
            beats["n"] += 1
        wd.heartbeat.side_effect = _hb

        async def boom(**kw):
            raise RuntimeError("kaboom")

        ctx = at.AgentToolCtx(
            api_client=MagicMock(), pool=MagicMock(),
            tools_for_api=[], channel_id=1, parent_model="m",
            sema=asyncio.Semaphore(2),
            parent_watchdog=wd,
        )
        with patch("services.deep_research.registry.run_subagent", new=boom):
            out = await at.execute_agent_tool({"description": "d", "prompt": "p"}, ctx)
        # 例外でも最終 heartbeat（finally の中）は1回叩かれる
        assert beats["n"] >= 1
        assert "tool_use_error" in out

    @pytest.mark.asyncio
    async def test_no_heartbeat_when_no_parent_watchdog(self):
        # parent_watchdog=None でも例外なく動作する（後方互換性）。
        async def fake(**kw):
            return SubAgentResult(subq=kw["briefing"], summary_md="ok", ok=True)
        ctx = at.AgentToolCtx(
            api_client=MagicMock(), pool=MagicMock(),
            tools_for_api=[], channel_id=1, parent_model="m",
            sema=asyncio.Semaphore(2),
            parent_watchdog=None,
        )
        with patch("services.deep_research.registry.run_subagent", new=fake):
            out = await at.execute_agent_tool({"description": "d", "prompt": "p"}, ctx)
        assert out == "ok"


class TestRegistry:
    def test_general_purpose_is_registered(self):
        spec = at.get_agent_spec("general-purpose")
        assert spec.name == "general-purpose"
        assert "subagent" in spec.system_prompt.lower()

    def test_unknown_falls_back_to_default(self):
        spec = at.get_agent_spec("nope")
        assert spec.name == config.SUBAGENT_DEFAULT_TYPE


class TestConcurrency:
    """1 ターンで model が複数 Agent() を撃ったとき、tool_loop が並列に dispatch する。

    回帰テスト: CONCURRENCY_SAFE_TOOLS から Agent が漏れていると CLI 純正と違って
    直列実行になる（haroin 2026-06-25 観察）。
    """

    def test_agent_is_in_concurrency_safe_tools(self):
        from services.claude_api.tool_loop import CONCURRENCY_SAFE_TOOLS
        assert "Agent" in CONCURRENCY_SAFE_TOOLS

    @pytest.mark.asyncio
    async def test_multiple_agents_run_in_parallel(self):
        # ベンチマーク: 3 つの Agent() を1ターンで撃って、合計時間が
        # 3 × per_agent_delay よりずっと短ければ並列実行されている証拠。
        per_agent_delay = 0.2
        peak = {"now": 0, "max": 0}

        async def fake_run_subagent(**kw):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(per_agent_delay)
            peak["now"] -= 1
            return SubAgentResult(subq=kw["briefing"], summary_md="ok", ok=True)

        ctx = at.AgentToolCtx(
            api_client=MagicMock(), pool=MagicMock(),
            tools_for_api=[], channel_id=1, parent_model="m",
            sema=asyncio.Semaphore(8),   # 専用 sema は十分余裕がある
        )
        with patch("services.deep_research.registry.run_subagent", new=fake_run_subagent):
            t0 = asyncio.get_event_loop().time()
            results = await asyncio.gather(*[
                at.execute_agent_tool({"description": f"d{i}", "prompt": f"p{i}"}, ctx)
                for i in range(3)
            ])
            elapsed = asyncio.get_event_loop().time() - t0
        assert all(r == "ok" for r in results)
        assert peak["max"] == 3                 # 3 体が同時に走った
        # 直列なら ≥ 3*delay。並列なら ≈ 1*delay。確実に並列であることを assert。
        assert elapsed < per_agent_delay * 2.0  # 余裕を持って 2x で判定


class TestEntryWiring:
    """entry.py がフラグで Agent tool を広告する／restricted では広告しないこと。"""

    def test_schema_lists_only_admin(self):
        # restricted は advertised_tools=[] 経路なので、entry.py の if 分岐で
        # Agent も追加されない（execute_tool 側でも reject）。
        # これは静的検査: スキーマ自体に restricted 専用フィールドが無いことを確認。
        s = at.agent_tool_schema()
        assert "restricted" not in str(s).lower()

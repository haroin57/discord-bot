"""Tests for services/claude_api/tool_catalog.py（Tool Search カタログ）と
mcp_selector の dispatcher / keyword 修正、errors の tool search 400 検知。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.claude_api import tool_catalog as tc
from services.claude_api.mcp_client import (
    STATIC_MCP_TOOL_SCHEMAS,
    mcp_tools_to_anthropic,
    static_mcp_tools_to_anthropic,
)
from services.claude_api.tools import builtin_tools_for_api


@pytest.fixture(autouse=True)
def _reset_runtime_flag():
    tc._reset_for_test()
    yield
    tc._reset_for_test()


def _fake_handle(key: str, tool_names: list[str]):
    return SimpleNamespace(
        key=key,
        tools=[
            SimpleNamespace(
                name=n, description=f"desc {n}",
                inputSchema={"type": "object", "properties": {}},
            )
            for n in tool_names
        ],
    )


_AGENT_SCHEMA = {"name": "Agent", "description": "subagent",
                 "input_schema": {"type": "object", "properties": {}}}


class TestBuildAdvertisedTools:
    def test_restricted_is_empty(self):
        assert tc.build_advertised_tools(
            restricted=True, live_handles={}, always_keys={"mem0"},
            hot_keys={"mem0"}, user_id=1, tool_search_enabled=True,
            agent_tool_schema=_AGENT_SCHEMA,
        ) == []

    def test_flag_off_matches_legacy_assembly(self):
        """フラグ OFF は従来実装（builtin + static(always) + live + agent）と
        byte-identical — 既存セッションの prompt cache を壊さない回帰固定。"""
        handles = {"x-scraper": _fake_handle("x-scraper", ["search_x"])}
        always = {"mem0", "brave-search"}
        got = tc.build_advertised_tools(
            restricted=False, live_handles=handles, always_keys=always,
            hot_keys=always | {"x-scraper"}, user_id=1,
            tool_search_enabled=False, agent_tool_schema=_AGENT_SCHEMA,
        )
        legacy = (
            builtin_tools_for_api(restricted=False)
            + static_mcp_tools_to_anthropic(always)
            + mcp_tools_to_anthropic(handles)
            + [_AGENT_SCHEMA]
        )
        assert got == legacy

    def test_flag_on_structure(self):
        handles = {"x-scraper": _fake_handle("x-scraper", ["search_x"])}
        hot = {"mem0", "x-scraper"}
        got = tc.build_advertised_tools(
            restricted=False, live_handles=handles, always_keys={"mem0"},
            hot_keys=hot, user_id=None, tool_search_enabled=True,
            agent_tool_schema=_AGENT_SCHEMA,
        )
        # 先頭は Tool Search Tool
        assert got[0]["type"].startswith("tool_search_tool_")
        by_name = {t.get("name"): t for t in got}
        # hot set は非 defer
        for t in got:
            name = t.get("name", "")
            if name.startswith("mcp__mem0__") or name == "mcp__x-scraper__search_x":
                assert not t.get("defer_loading"), name
        # defer カタログが存在し、hot 以外は defer
        deferred = [t for t in got if t.get("defer_loading")]
        assert deferred, "deferred catalog should not be empty"
        assert all(not t["name"].startswith("mcp__mem0__") for t in deferred)
        # dedupe: 同名 tool は一度だけ
        names = [t.get("name") for t in got]
        assert len(names) == len(set(names))
        # 非 defer が最低 1 つ（builtin）
        assert any(not t.get("defer_loading") for t in got)

    def test_flag_on_is_deterministic(self):
        kw = dict(
            restricted=False, live_handles={}, always_keys={"mem0"},
            hot_keys={"mem0"}, user_id=1, tool_search_enabled=True,
            agent_tool_schema=None,
        )
        assert tc.build_advertised_tools(**kw) == tc.build_advertised_tools(**kw)

    def test_permission_gated_servers_excluded(self):
        keys_anon = tc.deferred_catalog_keys(None)
        assert "realestate" not in keys_anon
        assert "nature-remo" not in keys_anon
        # 非ゲートサーバーは入る
        assert "x-scraper" in keys_anon

    def test_live_handle_wins_over_static(self):
        # static にも live にも居るサーバーは live schema が採用され重複しない
        key = next(iter(STATIC_MCP_TOOL_SCHEMAS))
        static_names = [t["name"] for t in STATIC_MCP_TOOL_SCHEMAS[key]]
        short = static_names[0].split("__", 2)[2]
        handles = {key: _fake_handle(key, [short])}
        got = tc.build_advertised_tools(
            restricted=False, live_handles=handles, always_keys=set(),
            hot_keys={key}, user_id=1, tool_search_enabled=True,
            agent_tool_schema=None,
        )
        matches = [t for t in got if t.get("name") == static_names[0]]
        assert len(matches) == 1
        assert matches[0]["description"] == f"desc {short}"


class TestRuntimeDisable:
    def test_disable_and_reset(self):
        assert tc.is_tool_search_active() is True
        tc.disable_tool_search("test")
        assert tc.is_tool_search_active() is False
        tc._reset_for_test()
        assert tc.is_tool_search_active() is True


class TestToolSearchErrorDetection:
    def _exc(self, status, message):
        exc = Exception("x")
        exc.status_code = status
        exc.body = {"error": {"type": "invalid_request_error",
                              "message": message}}
        return exc

    def test_detects_defer_loading_rejection(self):
        from services.claude_api.errors import is_tool_search_request_error
        assert is_tool_search_request_error(self._exc(
            400, "tools.5.defer_loading: Extra inputs are not permitted"))
        assert is_tool_search_request_error(self._exc(
            400, "Unknown tool type: tool_search_tool_regex_20251119"))

    def test_ignores_unrelated_400(self):
        from services.claude_api.errors import is_tool_search_request_error
        assert not is_tool_search_request_error(self._exc(
            400, "messages: roles must alternate"))
        assert not is_tool_search_request_error(self._exc(
            429, "rate limited tool_search"))


class TestSelectorDispatcher:
    @pytest.mark.asyncio
    async def test_keyword_mode(self, monkeypatch):
        import config
        from services import mcp_selector
        monkeypatch.setattr(config, "MCP_SELECTOR_MODE", "keyword")
        sem = AsyncMock()
        monkeypatch.setattr(
            "services.mcp_semantic_selector.pick_mcp_servers_semantic", sem,
        )
        out = await mcp_selector.select_mcp_servers("株価教えて", user_id=1)
        assert "nikkei-indexes" in out
        sem.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hybrid_union(self, monkeypatch):
        import config
        from services import mcp_selector
        monkeypatch.setattr(config, "MCP_SELECTOR_MODE", "hybrid")
        monkeypatch.setattr(
            "services.mcp_semantic_selector.pick_mcp_servers_semantic",
            AsyncMock(return_value={"scholarly": {"command": "x"}}),
        )
        out = await mcp_selector.select_mcp_servers("株価教えて", user_id=1)
        assert "nikkei-indexes" in out   # keyword 由来
        assert "scholarly" in out        # semantic 由来

    @pytest.mark.asyncio
    async def test_semantic_failure_falls_back_to_keyword(self, monkeypatch):
        import config
        from services import mcp_selector
        monkeypatch.setattr(config, "MCP_SELECTOR_MODE", "hybrid")
        monkeypatch.setattr(
            "services.mcp_semantic_selector.pick_mcp_servers_semantic",
            AsyncMock(side_effect=RuntimeError("openai down")),
        )
        out = await mcp_selector.select_mcp_servers("株価教えて", user_id=1)
        assert "nikkei-indexes" in out


class TestKeywordTriggerFixes:
    def test_pdocs_does_not_trigger_docs_tier(self):
        from services.mcp_selector import pick_mcp_servers
        out = pick_mcp_servers("READMEのpdocsが変です", user_id=1)
        assert "context7" not in out

    def test_docs_with_boundary_triggers(self):
        from services.mcp_selector import pick_mcp_servers
        out = pick_mcp_servers("read the docs. please", user_id=1)
        assert "context7" in out

    def test_matrix_word_does_not_trigger_web(self):
        from services.mcp_selector import pick_mcp_servers
        out = pick_mcp_servers("マトリックス では誰が主人公？", user_id=1)
        assert "x-scraper" not in out
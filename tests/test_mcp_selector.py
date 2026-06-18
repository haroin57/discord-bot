"""Tests for services.mcp_selector — keyword-triggered MCP server selection.

The actual server dicts live in config.py and depend on env-var
credentials; we patch them with stand-ins so the tests don't care which
optional servers are configured on the current host.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services import mcp_selector


_ADMIN_ID = 573489206240870410
_NON_ADMIN_ID = 999999999999999


@pytest.fixture(autouse=True)
def _stub_server_tiers():
    """Replace each tier dict with a unique sentinel server entry."""
    base = {"base": {"command": "base-cmd"}}
    web = {"web": {"command": "web-cmd"}}
    heavy = {"heavy": {"command": "heavy-cmd"}}
    memory = {"memory": {"command": "memory-cmd"}}
    paper = {"paper": {"command": "paper-cmd"}}
    docs = {"docs": {"command": "docs-cmd"}}
    realestate = {"realestate": {"command": "realestate-cmd"}}
    union = {**base, **web, **heavy, **memory, **paper, **docs, **realestate}
    with patch.multiple(
        mcp_selector,
        MCP_SERVERS_BASE=base,
        MCP_SERVERS_WEB=web,
        MCP_SERVERS_HEAVY=heavy,
        MCP_SERVERS_MEMORY=memory,
        MCP_SERVERS_PAPER=paper,
        MCP_SERVERS_DOCS=docs,
        MCP_SERVERS_REALESTATE=realestate,
        MCP_SERVERS=union,
        ADMIN_USER_IDS={_ADMIN_ID},
    ):
        yield


def test_empty_text_yields_only_base():
    out = mcp_selector.pick_mcp_servers("")
    assert set(out.keys()) == {"base"}


def test_none_text_safe():
    out = mcp_selector.pick_mcp_servers(None)  # type: ignore[arg-type]
    assert set(out.keys()) == {"base"}


def test_web_trigger_picks_web():
    out = mcp_selector.pick_mcp_servers("ニュース 検索して")
    assert "web" in out
    assert "heavy" not in out


def test_heavy_trigger_picks_playwright():
    out = mcp_selector.pick_mcp_servers("playwright で screenshot 撮って")
    assert "heavy" in out


def test_realestate_admin_with_trigger():
    out = mcp_selector.pick_mcp_servers("目黒区の物件探して", user_id=_ADMIN_ID)
    assert "realestate" in out


def test_realestate_admin_without_trigger():
    out = mcp_selector.pick_mcp_servers("今日の天気は？", user_id=_ADMIN_ID)
    assert "realestate" not in out


def test_realestate_non_admin_with_trigger_blocked():
    out = mcp_selector.pick_mcp_servers(
        "SUUMO で目黒区の物件探して", user_id=_NON_ADMIN_ID
    )
    assert "realestate" not in out


def test_realestate_legacy_call_no_user_id_blocked():
    """SDK legacy path は user_id を渡さない. デフォルト None で REALESTATE 含めない."""
    out = mcp_selector.pick_mcp_servers("目黒区の賃貸の家賃")
    assert "realestate" not in out


def test_memory_trigger_picks_mem0():
    out = mcp_selector.pick_mcp_servers("以前話したこと覚えてる？")
    assert "memory" in out


def test_memory_internal_module_trigger():
    """mem0 のソースコード言及でも memory tier が起きる (CLAUDE.md ルール)。"""
    out = mcp_selector.pick_mcp_servers("search.py の動作確認")
    assert "memory" in out


def test_paper_trigger_picks_scholarly():
    out = mcp_selector.pick_mcp_servers("arxiv で論文探したい")
    assert "paper" in out


def test_docs_trigger_picks_context7():
    out = mcp_selector.pick_mcp_servers("FastAPI の docs 教えて")
    assert "docs" in out


def test_multiple_triggers_compose():
    out = mcp_selector.pick_mcp_servers("playwright で github.com/ のドキュメント開いて")
    assert "heavy" in out
    assert "docs" in out


def test_case_insensitive_match():
    out = mcp_selector.pick_mcp_servers("BROWSE the site")
    assert "heavy" in out


def test_get_mcp_servers_by_keys_filters():
    out = mcp_selector.get_mcp_servers_by_keys(["web", "memory"])
    assert set(out.keys()) == {"web", "memory"}


def test_get_mcp_servers_by_keys_ignores_unknown():
    out = mcp_selector.get_mcp_servers_by_keys(["web", "does-not-exist"])
    assert set(out.keys()) == {"web"}


def test_config_no_longer_exposes_selector():
    """The dispatch logic has moved out of config.py to keep concerns separate."""
    import config as _cfg
    assert not hasattr(_cfg, "pick_mcp_servers")
    assert not hasattr(_cfg, "get_mcp_servers_by_keys")


class TestMarketDataRegistration:
    """market-data MCPs (nikkei-indexes / yfj-quote, 2026-06-13 plan)."""

    def test_finance_keywords_pick_web_tier(self):
        # The autouse fixture stubs tier dicts, so membership is asserted via
        # the sentinel "web" key (same pattern as test_web_trigger_picks_web).
        for text in ("トヨタの株価教えて", "日経VIどうなってる", "今日の相場は？"):
            result = mcp_selector.pick_mcp_servers(text)
            assert "web" in result, text

    def test_bare_vi_word_does_not_trigger(self):
        # "vi" was deliberately excluded from triggers (via/vim false fire).
        result = mcp_selector.pick_mcp_servers("vimの設定を直して")
        assert "web" not in result

    def test_config_registers_both_servers(self):
        import config
        for key in ("nikkei-indexes", "yfj-quote"):
            assert key in config.MCP_SERVERS_WEB
            cfg = config.MCP_SERVERS_WEB[key]
            assert cfg["command"].endswith(".venv/bin/python")
            assert cfg["args"][0].endswith("mcp_server.py")

    def test_static_schemas_carry_both_servers(self):
        import json
        from pathlib import Path
        schemas = json.loads(
            Path("services/claude_api/static_mcp_schemas.json").read_text()
        )
        assert len(schemas.get("nikkei-indexes", [])) == 3
        assert len(schemas.get("yfj-quote", [])) == 3
        names = {t["name"] for v in schemas.values() for t in v}
        assert "mcp__nikkei-indexes__check_vi_signal" in names
        assert "mcp__yfj-quote__get_quote" in names

    def test_always_advertised_includes_market_data(self):
        from services.claude_api.entry import ALWAYS_ADVERTISED_MCP_KEYS
        assert {"nikkei-indexes", "yfj-quote"} <= ALWAYS_ADVERTISED_MCP_KEYS

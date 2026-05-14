"""Tests for services.mcp_selector — keyword-triggered MCP server selection.

The actual server dicts live in config.py and depend on env-var
credentials; we patch them with stand-ins so the tests don't care which
optional servers are configured on the current host.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services import mcp_selector


@pytest.fixture(autouse=True)
def _stub_server_tiers():
    """Replace each tier dict with a unique sentinel server entry."""
    base = {"base": {"command": "base-cmd"}}
    web = {"web": {"command": "web-cmd"}}
    heavy = {"heavy": {"command": "heavy-cmd"}}
    memory = {"memory": {"command": "memory-cmd"}}
    paper = {"paper": {"command": "paper-cmd"}}
    docs = {"docs": {"command": "docs-cmd"}}
    union = {**base, **web, **heavy, **memory, **paper, **docs}
    with patch.multiple(
        mcp_selector,
        MCP_SERVERS_BASE=base,
        MCP_SERVERS_WEB=web,
        MCP_SERVERS_HEAVY=heavy,
        MCP_SERVERS_MEMORY=memory,
        MCP_SERVERS_PAPER=paper,
        MCP_SERVERS_DOCS=docs,
        MCP_SERVERS=union,
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

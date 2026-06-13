"""MCP server selection by user-text keyword scanning.

`pick_mcp_servers(raw_user_text)` returns the minimal set of MCP server
configs needed for a request. BASE is always loaded; WEB / HEAVY /
MEMORY / PAPER / DOCS tiers are added only when the user's raw text
mentions a trigger keyword. This avoids paying chromium-startup costs
or spinning up ChromaDB on every chat reply.

The server tier dicts and the union `MCP_SERVERS` live in `config.py`
since they bind to env-var credentials (BRAVE_API_KEY, TAVILY_API_KEY,
GITHUB_PERSONAL_ACCESS_TOKEN, etc.). This module is pure dispatch on
top of that config.
"""

from __future__ import annotations

from config import (
    MCP_SERVERS,
    MCP_SERVERS_BASE,
    MCP_SERVERS_DOCS,
    MCP_SERVERS_HEAVY,
    MCP_SERVERS_MEMORY,
    MCP_SERVERS_PAPER,
    MCP_SERVERS_WEB,
)

__all__ = ["get_mcp_servers_by_keys", "pick_mcp_servers"]


# Keyword sets used by pick_mcp_servers(). Cheap substring scans on the
# user's raw text — model is still free to refuse / pick differently, this
# only decides which subprocesses we spin up for the request.
# Tuples are pre-lowercased at module load so pick_mcp_servers() can compare
# without a per-call .lower() on each keyword.
_WEB_TRIGGERS = tuple(kw.lower() for kw in (
    "検索", "調べて", "ググって", "ニュース",
    "twitter", "x.com", "ツイート", "tweet", "trend", "トレンド",
    "search", "ググ", "Brave", "x ", "X(", "X検索",
    # Market-data tier (nikkei-indexes / yfj-quote) — eager spawn for finance
    # talk. NOTE: bare "vi" is deliberately absent (substring would misfire
    # on "via", "vim", "review", …); 日経vi covers the real usage.
    "株価", "日経", "日経vi", "市場", "相場", "チャート", "銘柄",
    "vix", "ドル円", "為替", "topix", "指数",
))
_HEAVY_TRIGGERS = tuple(kw.lower() for kw in (
    "ブラウズ", "browse", "スクリーンショット", "screenshot",
    "playwright", "puppeteer", "render", "webページを開",
    "サイトを開", "アクセスして",
))
_MEMORY_TRIGGERS = tuple(kw.lower() for kw in (
    "記憶", "覚えて", "覚えとい", "覚えてる", "思い出", "忘れた", "忘れない",
    "mem0", "memory", "remember", "recall", "過去の",
    "前に話した", "前に言った", "以前", "昔話した",
    # Internal-module references — when the user talks about the Mem0 code
    # (search.py / dedup / extract / ingest / mcp_mem0), they almost always
    # want to *test* it, which means kanae will need mcp__mem0__search.
    "search.py", "dedup", "extract.py", "ingest.py", "mcp__mem0",
    "mcp_mem0", "mem0_server",
    "記憶検索", "メモリ検索", "ベクトル検索", "セマンティック検索",
))
_PAPER_TRIGGERS = tuple(kw.lower() for kw in (
    # "scholar" without a qualifier was a false-positive risk (matches
    # "scholarship", "scholar's", etc.). Use disambiguated forms only.
    "論文", "paper", "arxiv", "学術", "引用文献", "citation",
    "google scholar", "semantic scholar", "scholar.google",
))
_DOCS_TRIGGERS = tuple(kw.lower() for kw in (
    # Library / framework docs lookup via context7 MCP. Includes English +
    # Japanese cues; pre-lowercased so pick_mcp_servers skips .lower() per kw.
    "context7", "docs", "ドキュメント", "公式ドキュメント", "api reference",
    "リファレンス", "ライブラリ", "library", "使い方", "how to use",
    "npm package", "pypi", "github.com/",
))


def get_mcp_servers_by_keys(keys: list[str]) -> dict:
    """Reconstruct MCP server config dict from a list of server name keys."""
    return {k: v for k, v in MCP_SERVERS.items() if k in keys}


def pick_mcp_servers(raw_user_text: str) -> dict:
    """Return the minimal MCP server set needed for this request.

    BASE is always loaded. WEB / HEAVY / MEMORY tiers are added only when the
    user text actually mentions something that would need them. The huge win
    is NOT loading playwright (~500MB chromium) or ChromaDB on every chat
    reply.
    """
    text = (raw_user_text or "").lower()
    servers = dict(MCP_SERVERS_BASE)
    if any(kw in text for kw in _WEB_TRIGGERS):
        servers.update(MCP_SERVERS_WEB)
    if any(kw in text for kw in _HEAVY_TRIGGERS):
        servers.update(MCP_SERVERS_HEAVY)
    if any(kw in text for kw in _MEMORY_TRIGGERS):
        servers.update(MCP_SERVERS_MEMORY)
    if any(kw in text for kw in _PAPER_TRIGGERS):
        servers.update(MCP_SERVERS_PAPER)
    if any(kw in text for kw in _DOCS_TRIGGERS):
        servers.update(MCP_SERVERS_DOCS)
    return servers

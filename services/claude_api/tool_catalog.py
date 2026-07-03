"""Advertised-tools catalog builder (Tool Search + defer_loading, ADR 参照).

従来は「keyword selector が選んだサーバーだけをモデルに見せる」方式で、選択漏れ
＝そのターンでツールが存在しないのと同じだった。本モジュールは Anthropic の
server-side Tool Search Tool を使い、

  - hot set（builtin + always-advertised + セッション sticky + selector 選択分）
    は従来どおり非 defer で即時可視
  - それ以外の全 MCP tool schema を ``defer_loading: true`` で常時同梱
    （prompt prefix には載らない = cache 安全。モデルが検索して必要な分だけロード）

という「常時コア + 検索で動的ロード」構成の tools 配列を組み立てる。

フラグ OFF（``TOOL_SEARCH_ENABLED=false`` またはランタイム auto-off）時は
従来の組み立てと byte-identical な配列を返す（既存セッションの prompt cache を
壊さない）。auto-off は subscriber OAuth 経路で tool search が 400 になった場合の
graceful degrade（entry.py 側で検知して disable_tool_search() を呼ぶ）。
"""

from __future__ import annotations

import logging

from config import (
    ADMIN_USER_IDS,
    ADVISOR_ENABLED,
    ADVISOR_MODEL,
    NATURE_REMO_ALLOWED_USER_IDS,
    TOOL_SEARCH_VARIANT,
)
from services.claude_api.mcp_client import (
    STATIC_MCP_TOOL_SCHEMAS,
    mcp_tools_to_anthropic,
    static_mcp_tools_to_anthropic,
)
from services.claude_api.tools import builtin_tools_for_api

log = logging.getLogger(__name__)


def tool_search_tool() -> dict:
    """Tool Search Tool の定義（regex / bm25 は config で切替）。"""
    variant = TOOL_SEARCH_VARIANT if TOOL_SEARCH_VARIANT in ("regex", "bm25") else "regex"
    return {
        "type": f"tool_search_tool_{variant}_20251119",
        "name": f"tool_search_tool_{variant}",
    }


def advisor_tool() -> dict:
    """Anthropic server-side Advisor tool（beta, 熟考 P4）。

    モデルが会話中に別モデル（既定 Opus 4.8）へ相談できる。beta header は
    client.py の MODEL_BETAS 側で ADVISOR_ENABLED 時のみ付与される。
    """
    return {
        "type": "advisor_20260301",
        "name": "advisor",
        "model": ADVISOR_MODEL,
    }


# --- ランタイム無効化状態（プロセス寿命） ---------------------------------
# API が tool search / defer_loading を 400 で拒否した場合に entry.py が
# disable_tool_search() を呼び、以後のリクエストは従来組み立てへ degrade する。
# 再有効化はプロセス再起動（意図的にシンプルに保つ）。
_runtime_disabled = False
_disabled_reason: str | None = None


def is_tool_search_active() -> bool:
    return not _runtime_disabled


def disable_tool_search(reason: str) -> None:
    global _runtime_disabled, _disabled_reason
    if not _runtime_disabled:
        log.warning("tool search runtime-disabled: %s", reason)
    _runtime_disabled = True
    _disabled_reason = reason


def _reset_for_test() -> None:
    global _runtime_disabled, _disabled_reason
    _runtime_disabled = False
    _disabled_reason = None


# --- permission gate --------------------------------------------------------
# keyword selector（services/mcp_selector.py）の user ゲートと同一条件。
# defer 広告は「モデルから発見可能」を意味するので、ゲートは fail-closed。
_GATED_SERVERS = ("realestate", "nature-remo")


def _server_allowed(key: str, user_id: int | None) -> bool:
    if key == "realestate":
        return user_id is not None and user_id in ADMIN_USER_IDS
    if key == "nature-remo":
        return user_id is not None and user_id in NATURE_REMO_ALLOWED_USER_IDS
    return True


def deferred_catalog_keys(user_id: int | None) -> set[str]:
    """defer 広告してよい MCP server key 集合（permission ゲート適用後）。

    static schema 未収録のサーバーは defer 対象にできない（検索カタログに
    schema が無い）ため返さない — dump 拡充までは従来の keyword ゲートが継続
    する段階的移行。
    """
    return {
        key for key in STATIC_MCP_TOOL_SCHEMAS
        if _server_allowed(key, user_id)
    }


def build_advertised_tools(
    *,
    restricted: bool,
    live_handles: dict,
    always_keys: set[str],
    hot_keys: set[str],
    user_id: int | None,
    tool_search_enabled: bool,
    agent_tool_schema: dict | None,
) -> list[dict]:
    """モデルへ広告する tools 配列を組み立てる。

    ``tool_search_enabled=False`` のとき、返す配列は従来実装
    （builtin + static(always) + live handles + agent tool）と byte-identical。
    True のとき、先頭に Tool Search Tool、hot set（非 defer）、残り全カタログを
    ``defer_loading: true`` で同梱する。defer 集合は (server, tool名) ソートで
    バイト決定的（prompt cache の prefix 安定性のため）。
    """
    if restricted:
        return []

    if not tool_search_enabled:
        out = (
            builtin_tools_for_api(restricted=False)
            + static_mcp_tools_to_anthropic(always_keys)
            + mcp_tools_to_anthropic(live_handles)
        )
        if agent_tool_schema is not None:
            out = out + [agent_tool_schema]
        if ADVISOR_ENABLED:
            out = out + [advisor_tool()]
        return out

    tools: list[dict] = [tool_search_tool()]
    if ADVISOR_ENABLED:
        tools.append(advisor_tool())
    tools += builtin_tools_for_api(restricted=False)
    if agent_tool_schema is not None:
        tools.append(agent_tool_schema)

    seen: set[str] = {t.get("name", "") for t in tools}

    # hot set（非 defer）: live handle 優先、無ければ static schema。
    for t in mcp_tools_to_anthropic(live_handles):
        if t["name"] not in seen:
            seen.add(t["name"])
            tools.append(t)
    for t in static_mcp_tools_to_anthropic(sorted(hot_keys)):
        if t["name"] not in seen:
            seen.add(t["name"])
            tools.append(t)

    # deferred catalog: 残り全部を defer_loading 付きで（決定的順序）。
    defer_keys = sorted(deferred_catalog_keys(user_id) - set(hot_keys))
    for key in defer_keys:
        for tool in sorted(
            STATIC_MCP_TOOL_SCHEMAS.get(key, []),
            key=lambda t: t.get("name", ""),
        ):
            name = tool.get("name", "")
            if name in seen:
                continue
            seen.add(name)
            deferred = dict(tool)
            deferred["defer_loading"] = True
            tools.append(deferred)

    # API 制約: 少なくとも1つは非 defer が必要。builtin が常に居るので構造的に
    # 満たされるが、将来の変更に備えて防御的に確認する。
    assert any(not t.get("defer_loading") for t in tools), (
        "advertised tools must contain at least one non-deferred tool"
    )
    return tools

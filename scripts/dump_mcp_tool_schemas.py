"""Dump MCP tool schemas (Anthropic shape) for the WEB-tier MCP servers.

Spawns each server via stdio_client, calls list_tools(), and prints a
Python literal that can be pasted into STATIC_MCP_TOOL_SCHEMAS.
"""

import asyncio
import json
import os
import pprint
import sys
from contextlib import AsyncExitStack
from pathlib import Path

# Reuse the bot's venv path so MCP SDK + bot deps are available.
sys.path.insert(0, "/home/ubuntu/discord-bot")

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


# 対象は config.MCP_SERVERS の全サーバー（tier 横断）。手書きの部分集合を
# やめて、defer_loading カタログ（Tool Search）に全サーバーが載れるようにする。
# config import は .env 読み込みと dir 作成の副作用を持つが、スクリプト用途では
# 許容する。
from config import MCP_SERVERS as SERVERS  # noqa: E402


async def dump_server(name: str, cfg: dict) -> list[dict]:
    async with AsyncExitStack() as stack:
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args") or [],
            env={**os.environ, **(cfg.get("env") or {})},
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        resp = await session.list_tools()
        tools_out = []
        for tool in resp.tools:
            tools_out.append({
                "name": f"mcp__{name}__{tool.name}",
                "description": (tool.description or ""),
                "input_schema": (
                    tool.inputSchema if tool.inputSchema
                    else {"type": "object", "properties": {}}
                ),
            })
        return tools_out


_STATIC_JSON = (
    Path(__file__).resolve().parent.parent
    / "services" / "claude_api" / "static_mcp_schemas.json"
)

# 生 dump は Brave のグローバル既定 (US/en) のままなので、日本語 bot 向けに
# 再生成のたびここで上書きする。default はモデルへのヒントに過ぎず、省略時の
# 実挙動は Brave サーバー側で決まるため、description にも明示指定を促す。
_JP_HINT = (
    " 日本語のクエリでは country='JP', search_lang='jp', ui_lang='ja-JP' を"
    "明示的に指定すること（省略するとUS/英語の検索結果になる）。"
)
_FRESHNESS_PATTERN = r"^(pd|pw|pm|py|\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2})$"

# サーバー単位の「いつ呼ぶか」ヒント。tool description の末尾に付与する。
# Anthropic のガイダンス（prescriptive trigger conditions in descriptions give
# measurable lift in should-call rate）に基づく。Tool Search（regex/BM25）の
# 検索対象テキストにもなるため、日本語キーワードを含めることが検索精度にも効く。
_TRIGGER_HINTS: dict[str, str] = {
    "x-scraper": (
        "X(Twitter)の投稿検索・トレンド・特定ポスト取得が必要なときに呼ぶ。"
        "速報・一次情報・現場の声を拾う用途に向く。"
    ),
    "brave-search": (
        "Web検索が必要なとき（ニュース・時事・技術情報・一般調査）に呼ぶ。"
    ),
    "nikkei-indexes": (
        "日経平均・日経VI・国内株価指数の現在値/履歴/VIシグナルを聞かれたら呼ぶ。"
    ),
    "yfj-quote": (
        "個別株・ETF・為替レートの現在値/履歴（Yahoo Finance Japan）や"
        "Fisco VIニュースが必要なときに呼ぶ。"
    ),
    "polymarket": (
        "予測市場（Polymarket）のオッズ・確率・マーケット情報を聞かれたら呼ぶ。"
        "選挙・経済イベント・スポーツ等の発生確率の参照に向く。"
    ),
    "minkabu-fx": (
        "FX（ドル円等）のオーダーブック・注文状況・為替介入の観測が"
        "必要なときに呼ぶ。"
    ),
    "scholarly": (
        "学術論文・研究文献の検索・引用情報が必要なときに呼ぶ。"
    ),
    "context7": (
        "ライブラリ/フレームワークの公式ドキュメント・APIリファレンスを"
        "正確に参照したいときに呼ぶ。"
    ),
    "tavily": (
        "Web検索（brave-searchの代替）が必要なときに呼ぶ。"
    ),
    "google-maps": (
        "地図・場所・店舗・経路・距離の情報が必要なときに呼ぶ。"
    ),
    "playwright": (
        "実ブラウザでのWebページ表示確認・スクリーンショット・フォーム操作が"
        "必要なときだけ呼ぶ（Chromium起動を伴い重い。単なる本文取得なら"
        "Web検索系で足りる）。"
    ),
    "realestate": (
        "賃貸・不動産物件の検索（SUUMO/HOMES）が必要なときに呼ぶ。"
    ),
    "nature-remo": (
        "自宅家電（エアコン・照明等）の操作・状態確認を頼まれたときに呼ぶ。"
    ),
}


def apply_description_overrides(out: dict[str, list[dict]]) -> None:
    """再生成のたびに適用する description / schema の後処理。

    1. brave-search: default を日本向けに寄せ、freshness のプレースホルダー
       enum（'YYYY-MM-DDtoYYYY-MM-DD' がリテラル値で実際の日付範囲が
       jsonschema 検証で弾かれる）を pattern に置き換える。
    2. 全サーバー: _TRIGGER_HINTS の「いつ呼ぶか」を description 末尾に付与。
    """
    for tool in out.get("brave-search", []):
        props = (tool.get("input_schema") or {}).get("properties") or {}
        for key, default in (
            ("country", "JP"), ("search_lang", "jp"), ("ui_lang", "ja-JP"),
        ):
            spec = props.get(key)
            if isinstance(spec, dict) and "default" in spec:
                enum = spec.get("enum")
                if enum is None or default in enum:
                    spec["default"] = default
        fresh = props.get("freshness")
        if isinstance(fresh, dict) and "enum" in fresh:
            fresh.pop("enum", None)
            fresh["pattern"] = _FRESHNESS_PATTERN
        desc = tool.get("description") or ""
        if desc and _JP_HINT not in desc:
            tool["description"] = desc.rstrip() + _JP_HINT

    for server_key, tools in out.items():
        hint = _TRIGGER_HINTS.get(server_key)
        if not hint:
            continue
        suffix = f"【いつ呼ぶか】{hint}"
        for tool in tools:
            desc = (tool.get("description") or "").rstrip()
            if suffix not in desc:
                tool["description"] = (desc + " " + suffix).strip()


# 後方互換 alias（旧名）。
apply_jp_overrides = apply_description_overrides


def _load_existing() -> dict[str, list[dict]]:
    try:
        with _STATIC_JSON.open(encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


async def main() -> None:
    existing = _load_existing()
    out: dict[str, list[dict]] = {}
    for name, cfg in SERVERS.items():
        try:
            tools = await asyncio.wait_for(dump_server(name, cfg), timeout=30)
        except Exception as exc:
            tools = []
            print(f"# {name}: FAILED {exc!r}", file=sys.stderr)
        if tools:
            out[name] = tools
            print(f"# {name}: {len(tools)} tools", file=sys.stderr)
        elif existing.get(name):
            # 失敗 / 空応答のサーバーは既存エントリを温存する。空リストを書くと
            # 後続の merge で正常だったスキーマが空に置き換わる事故になる。
            out[name] = existing[name]
            print(
                f"# {name}: keeping {len(existing[name])} existing tools",
                file=sys.stderr,
            )
    apply_description_overrides(out)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

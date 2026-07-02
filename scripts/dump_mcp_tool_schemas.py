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


SERVERS = {
    "x-scraper": {
        "command": "/home/ubuntu/free-code/x-scraper/.venv/bin/python",
        "args": ["/home/ubuntu/free-code/x-scraper/mcp_server.py"],
        "env": {},
    },
    "brave-search": {
        "command": "npx",
        "args": ["-y", "@brave/brave-search-mcp-server@2.0.80"],
        "env": {"BRAVE_API_KEY": os.environ.get("BRAVE_API_KEY", "")},
    },
    "nikkei-indexes": {
        "command": "/home/ubuntu/mcp-servers/nikkei-indexes/.venv/bin/python",
        "args": ["/home/ubuntu/mcp-servers/nikkei-indexes/mcp_server.py"],
        "env": {},
    },
    "yfj-quote": {
        "command": "/home/ubuntu/mcp-servers/yfj-quote/.venv/bin/python",
        "args": ["/home/ubuntu/mcp-servers/yfj-quote/mcp_server.py"],
        "env": {},
    },
    "polymarket": {
        "command": "/home/ubuntu/mcp-servers/yfj-quote/.venv/bin/python",
        "args": ["/home/ubuntu/mcp-servers/polymarket/mcp_server.py"],
        "env": {},
    },
    "minkabu-fx": {
        "command": "/home/ubuntu/mcp-servers/yfj-quote/.venv/bin/python",
        "args": ["/home/ubuntu/mcp-servers/minkabu-fx/mcp_server.py"],
        "env": {},
    },
}


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


def apply_jp_overrides(out: dict[str, list[dict]]) -> None:
    """brave-search 系ツールの default を日本向けに寄せ、freshness の
    プレースホルダー enum（'YYYY-MM-DDtoYYYY-MM-DD' がリテラル値になっていて
    実際の日付範囲が jsonschema 検証で弾かれる）を pattern に置き換える。"""
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
    apply_jp_overrides(out)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

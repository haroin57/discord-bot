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
    "news-curator": {
        "command": "/home/ubuntu/news-curator/.venv/bin/python",
        "args": ["/home/ubuntu/news-curator/mcp_server.py"],
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


async def main() -> None:
    out: dict[str, list[dict]] = {}
    for name, cfg in SERVERS.items():
        try:
            tools = await asyncio.wait_for(dump_server(name, cfg), timeout=30)
            out[name] = tools
            print(f"# {name}: {len(tools)} tools", file=sys.stderr)
        except Exception as exc:
            print(f"# {name}: FAILED {exc!r}", file=sys.stderr)
            out[name] = []
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

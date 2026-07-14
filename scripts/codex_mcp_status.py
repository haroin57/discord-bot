#!/usr/bin/env python3
"""Verify Codex app-server MCP connectivity without exposing tool results."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _expected_servers() -> set[str]:
    from services.codex_app_server.manager import configured_mcp_endpoints

    return set(configured_mcp_endpoints())


async def _run() -> dict[str, Any]:
    from config import CODEX_APP_SERVER_MODEL, CODEX_APP_SERVER_PATH, CODEX_BIN
    from services.codex_app_server.client import AsyncCodexAppServerClient
    from services.codex_app_server.manager import (
        build_app_server_extra_args,
        build_app_server_global_args,
    )

    expected = _expected_servers()
    client = AsyncCodexAppServerClient(
        codex_bin=CODEX_BIN,
        global_args=build_app_server_global_args(),
        extra_args=build_app_server_extra_args(CODEX_APP_SERVER_MODEL),
        env={"PATH": CODEX_APP_SERVER_PATH},
    )
    try:
        await client.start()
        await client.initialize(
            client_name="kanae_discord_mcp_canary",
            client_title="Kanae Discord MCP Canary",
            client_version="1.0",
            capabilities={},
        )
        account = await client.request("account/read", {}, timeout=15)
        account_type = str((account.get("account") or {}).get("type") or "")
        response = await client.request(
            "mcpServerStatus/list",
            {"detail": "full", "limit": 100},
            timeout=60,
        )
        raw_servers = response.get("data") or []
        servers: list[dict[str, Any]] = []
        present: set[str] = set()
        empty: list[str] = []
        unexpected: list[dict[str, Any]] = []
        for raw in raw_servers:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "")
            if not name:
                continue
            tools = raw.get("tools") or {}
            tool_names = (
                sorted(str(tool) for tool in tools)
                if isinstance(tools, dict)
                else []
            )
            present.add(name)
            if name in expected and not tool_names:
                empty.append(name)
            if name not in expected and tool_names:
                unexpected.append({"name": name, "tool_count": len(tool_names)})
                continue
            info = raw.get("serverInfo") or {}
            servers.append({
                "name": name,
                "auth_status": raw.get("authStatus"),
                "tool_count": len(tool_names),
                "tool_names": tool_names,
                "resource_count": len(raw.get("resources") or []),
                "resource_template_count": len(raw.get("resourceTemplates") or []),
                "server_implementation": str(info.get("name") or ""),
                "server_version": str(info.get("version") or ""),
            })
        missing = sorted(expected - present)
        return {
            "ok": (
                account_type == "chatgpt"
                and not missing
                and not empty
                and not unexpected
            ),
            "account_type": account_type,
            "expected_servers": sorted(expected),
            "missing_servers": missing,
            "empty_tool_servers": sorted(empty),
            "unexpected_tool_servers": sorted(
                unexpected, key=lambda item: item["name"],
            ),
            "servers": sorted(servers, key=lambda item: item["name"]),
        }
    finally:
        await client.close()


def main() -> None:
    result = asyncio.run(_run())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

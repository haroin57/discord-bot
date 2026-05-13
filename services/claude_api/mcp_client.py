"""MCP subprocess pool + tool dispatch for the direct-API path.

Wraps the Python ``mcp`` SDK so the tool_loop can advertise MCP-provided
tools to the Anthropic API and dispatch calls back to the right server.
The CLI used to own this lifecycle; in the direct-API world we own it.

Lifecycle is managed via ``AsyncExitStack``: ``spawn_servers()`` returns
a context-manager-shaped wrapper; the tool_loop's entry function should
own that stack so a crash anywhere in the loop guarantees ``aclose()``
is called and child PIDs SIGTERMed.

PID tracking is best-effort — stdio_client does not expose its subprocess
handle, so ``kill_orphan_mcp_processes()`` falls back to the same cmdline
substring scan as services/claude_sdk.py (``_MCP_CMDLINE_PATTERNS``).
"""

import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# streamable_http is optional / version-dependent — guard the import.
try:
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore
    _HAS_HTTP_TRANSPORT = True
except ImportError:  # pragma: no cover
    streamablehttp_client = None  # type: ignore
    _HAS_HTTP_TRANSPORT = False

log = logging.getLogger(__name__)

# Cmdline substring patterns for orphan MCP cleanup. Kept in sync with
# services/claude_sdk.py:245-260 so the cross-backend cleanup catches both
# CLI-spawned and direct-API-spawned MCP children during the migration.
_MCP_CMDLINE_PATTERNS = (
    "mcp-server-",
    "@modelcontextprotocol/server-",
    "@playwright/mcp",
    "playwright-mcp",
    "brave-search-mcp",
    "@upstash/context7-mcp",
    "context7-mcp",
    "x-scraper/mcp_server.py",
    "scholarly-mcp/server.py",
    "news-curator/mcp_server.py",
    "papermcp",
    "tavily-mcp",
)


@dataclass
class McpServerHandle:
    """Live MCP server connection + its tool list."""

    key: str
    session: ClientSession
    tools: list[Any] = field(default_factory=list)  # mcp.Tool


async def _spawn_one(
    stack: AsyncExitStack,
    name: str,
    cfg: dict,
) -> McpServerHandle:
    """Open transport, initialize session, list tools. Registers exits in stack."""
    if cfg.get("type") == "http":
        if not _HAS_HTTP_TRANSPORT:
            raise RuntimeError(
                f"MCP HTTP transport not available, cannot spawn '{name}'",
            )
        ctx = streamablehttp_client(  # type: ignore[misc]
            cfg["url"],
            headers=cfg.get("headers") or {},
        )
        transport_result = await stack.enter_async_context(ctx)
        # streamablehttp_client yields (read, write, _close) in current SDK
        read, write = transport_result[0], transport_result[1]
    else:
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args") or [],
            env={**os.environ, **(cfg.get("env") or {})},
        )
        read, write = await stack.enter_async_context(stdio_client(params))

    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    tools_resp = await session.list_tools()
    return McpServerHandle(key=name, session=session, tools=tools_resp.tools)


async def spawn_servers(
    cfg: dict[str, dict],
    stack: AsyncExitStack,
) -> dict[str, McpServerHandle]:
    """Spawn every server in ``cfg``; caller owns ``stack`` for cleanup."""
    handles: dict[str, McpServerHandle] = {}
    for name, server_cfg in cfg.items():
        try:
            handles[name] = await _spawn_one(stack, name, server_cfg)
            log.info("MCP spawned: %s (%d tools)", name, len(handles[name].tools))
        except Exception as exc:
            log.error("MCP spawn failed for %s: %s", name, exc)
            # Continue with the other servers; the user may still get useful
            # behavior with a subset (mirrors claude_sdk behavior, where the
            # CLI also tolerated single-server failures).
    return handles


def mcp_tools_to_anthropic(handles: dict[str, McpServerHandle]) -> list[dict]:
    """Convert MCP Tool definitions into the Anthropic tools-API schema."""
    out: list[dict] = []
    for h in handles.values():
        for tool in h.tools:
            qualified_name = f"mcp__{h.key}__{tool.name}"
            out.append({
                "name": qualified_name,
                "description": getattr(tool, "description", "") or "",
                "input_schema": (
                    getattr(tool, "inputSchema", None)
                    or {"type": "object", "properties": {}}
                ),
            })
    return out


async def call_mcp_tool(
    handles: dict[str, McpServerHandle],
    qualified_name: str,
    args: dict,
) -> str:
    """Dispatch a ``mcp__server__tool`` call to the right session.

    Stringifies the response content blocks (TextContent / ImageContent /
    EmbeddedResource) since Anthropic's ``tool_result.content`` is a string.
    """
    try:
        _, server, tool_name = qualified_name.split("__", 2)
    except ValueError as exc:
        raise ValueError(
            f"malformed MCP tool name {qualified_name!r}, want mcp__server__tool",
        ) from exc

    handle = handles.get(server)
    if handle is None:
        raise KeyError(f"MCP server '{server}' is not spawned")

    result = await handle.session.call_tool(tool_name, args)

    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", "") or "")
        elif block_type == "image":
            mime = getattr(block, "mimeType", "?")
            data = getattr(block, "data", "") or ""
            parts.append(f"[image: {mime}, {len(data)} bytes base64]")
        else:
            # Unknown / embedded resource — fall back to repr.
            parts.append(str(block))
    return "\n".join(parts) or "(empty)"


def kill_orphan_mcp_processes() -> int:
    """SIGTERM → SIGKILL any descendant MCP-server processes.

    Best-effort defense-in-depth in case AsyncExitStack didn't get to clean
    them up (process crash mid-loop, etc.). Cmdline-substring based so it
    survives the npx wrapper / venv-python variations.
    """
    if psutil is None:
        return 0
    killed = 0
    try:
        me = psutil.Process(os.getpid())
        targets = []
        for child in me.children(recursive=True):
            try:
                cmdline = " ".join(child.cmdline() or []).lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if any(pat in cmdline for pat in _MCP_CMDLINE_PATTERNS):
                targets.append(child)
        if not targets:
            return 0
        for p in targets:
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass
        gone, alive = psutil.wait_procs(targets, timeout=2)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        killed = len(targets)
    except Exception as exc:
        log.debug("kill_orphan_mcp_processes failed: %s", exc)
    return killed

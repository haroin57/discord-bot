"""Claude Code UserPromptSubmit hook search, served by the shared mem0 MCP process.

The hook used to have its own daemon (``mem0_hook_daemon.py``) that loaded
the full Mem0 stack (SDK, Chroma client, BM25/fact stores and the v1
associative index), which cost about 300MB resident just for this one query
shape. The search now runs inside ``mcp-mem0-shared``, which already holds
that stack. The daemon is a stdlib-only proxy from its Unix socket to this
route.

The route listens on 127.0.0.1 only, on the same port as the MCP ``search``
tool. FastMCP's DNS-rebinding guard covers only the MCP transport, not custom
routes, so the route makes its own checks: Host must be loopback, there must
be no Origin header, and X-Kanae-Hook is required. That custom header means a
browser cannot send a CORS-simple request, so a web page reached by the
headless renderer or by DNS rebinding cannot read memories through it.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("mem0-mcp.hook")

HOOK_ROUTE = "/hook/search"
HOOK_USER_ID = "573489206240870410"
HOOK_LIMIT = 4
MAX_QUERY_BYTES = 16_384
HOOK_HEADER = "x-kanae-hook"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _is_trusted_request(headers: Any) -> bool:
    if headers.get(HOOK_HEADER) != "1" or headers.get("origin") is not None:
        return False
    host = (headers.get("host") or "").lower()
    name = (
        host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    )
    return name in _LOOPBACK_HOSTS


async def hook_search_block(query: str) -> str:
    """Return the prompt block for ``query``; failures degrade to ``""``.

    Arguments match the former daemon exactly, so the injected block is
    unchanged.
    """
    if not query.strip():
        return ""
    from services.mem0.format import format_memories_for_prompt
    from services.mem0.search import search_memories_smart

    try:
        results = await search_memories_smart(
            query,
            HOOK_USER_ID,
            limit=HOOK_LIMIT,
            rewrite=False,
            rerank=False,
            include_core=True,
        )
        return format_memories_for_prompt(results) or ""
    except Exception:
        log.warning("hook search failed", exc_info=True)
        return ""


def register_hook_route(mcp: Any) -> None:
    """Attach ``POST /hook/search`` (raw UTF-8 query in, text block out)."""
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse

    @mcp.custom_route(HOOK_ROUTE, methods=["POST"], include_in_schema=False)
    async def _hook_search(request: Request) -> PlainTextResponse:
        if not _is_trusted_request(request.headers):
            return PlainTextResponse("forbidden", status_code=403)
        raw = await request.body()
        query = raw[:MAX_QUERY_BYTES].decode("utf-8", "replace").strip()
        block = await hook_search_block(query)
        return PlainTextResponse(block, media_type="text/plain; charset=utf-8")

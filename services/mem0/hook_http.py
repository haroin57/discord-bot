"""Claude Code UserPromptSubmit hook search, served by the shared mem0 MCP process.

The hook used to have its own daemon (``mem0_hook_daemon.py``) that loaded
the full Mem0 stack (SDK, Chroma client, BM25/fact stores and the v1
associative index), which cost about 300MB resident just for this one query
shape. The search now runs inside ``mcp-mem0-shared``, which already holds
that stack. The daemon is a stdlib-only proxy from its Unix socket to this
route.

The route listens on 127.0.0.1 only, on the same port as the MCP ``search``
tool. That tool already accepts any user_id without auth, so this route
exposes nothing new.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("mem0-mcp.hook")

HOOK_ROUTE = "/hook/search"
HOOK_USER_ID = "573489206240870410"
HOOK_LIMIT = 4
MAX_QUERY_BYTES = 16_384


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
        raw = await request.body()
        query = raw[:MAX_QUERY_BYTES].decode("utf-8", "replace").strip()
        block = await hook_search_block(query)
        return PlainTextResponse(block, media_type="text/plain; charset=utf-8")

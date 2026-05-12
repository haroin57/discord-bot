"""MCP server wrapping the Mem0 long-term memory store used by the Discord bot.

Exposes Mem0 search/add/list/delete as MCP tools so Claude Code can read and
write the same ChromaDB that かなえ Discord bot uses. Default user_id is
hardcoded to 先輩's Discord ID so記憶がそのまま共有される。

Reuses discord-bot's services.mem0_service which handles OAuth + config.
"""

import asyncio
import logging
import os
import re
import sys

# Discord snowflake IDs are 17-19 digits today and can grow to 22 within the
# 64-bit unsigned range. Accept that range as the user_id format gate so we
# don't let arbitrary strings flow into the Mem0 user-key dimension.
_USER_ID_PATTERN = re.compile(r"^\d{15,22}$")

# Upper bound on per-call limit so a caller cannot ask for a 999999-row
# result that exhausts memory and stalls the MCP server.
_MAX_LIMIT = 500


def _validated_user_id(user_id: str) -> str:
    """Reject malformed user_id strings before they reach Mem0."""
    if not isinstance(user_id, str) or not _USER_ID_PATTERN.match(user_id):
        raise ValueError(
            f"invalid user_id (expected 15-22 digit Discord snowflake): {user_id!r}"
        )
    return user_id


def _clamped_limit(limit: int) -> int:
    """Clamp limit into [1, _MAX_LIMIT]."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, _MAX_LIMIT))

# Ensure discord-bot root is on path so we can import services.*
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

# Load .env (OPENAI_API_KEY etc.)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_BOT_DIR, ".env"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from services.mem0_service import (  # noqa: E402
    add_memories,
    delete_memory,
    get_all_memories,
    search_memories,
    search_memories_smart,
)

# Log to stderr only — stdout is reserved for MCP protocol
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s mem0-mcp: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mem0-mcp")

# 先輩のDiscord ID — Mem0はuser_idでレコードを分けるので、
# Discord botとClaude Codeで同じIDを使えば記憶を共有できる
DEFAULT_USER_ID = "573489206240870410"


def _format_for_mcp(memories: list[dict], limit: int | None = None) -> str:
    """MCP-safe formatter without bracketed boundary markers.

    The previous formatter used "[Mem0 — ...]" / "[End of Mem0 memories]"
    which Claude's CLI sometimes interpreted as a stop boundary, causing
    the assistant to halt after receiving the tool result.
    """
    lines = []
    for m in memories:
        text = m.get("memory", m.get("text", ""))
        if text:
            lines.append(f"- {text}")
    if limit is not None and len(lines) > limit:
        truncated = len(lines) - limit
        lines = lines[:limit]
        lines.append(f"(+{truncated} more — narrow your query to see them)")
    return "\n".join(lines) if lines else ""


mcp = FastMCP("mem0")


@mcp.tool()
async def search(
    query: str, limit: int = 5,
    user_id: str = DEFAULT_USER_ID, smart: bool = True,
) -> str:
    """Semantic search against the shared Mem0 long-term memory.

    Returns a formatted block of relevant memories for the given query.
    Use this when you want to recall facts about 先輩 (preferences,
    past decisions, ongoing projects, personal details).

    When `smart=True` (default), the query is first rewritten by Haiku into
    canonical-keyword variants, searched in parallel, and the merged candidates
    are LLM-reranked. Disable with `smart=False` for plain vector search.

    Args:
        query: Natural language query describing what you want to recall.
        limit: Max number of memories to return (default 5).
        user_id: Mem0 user key. Defaults to 先輩's Discord ID so results
            are shared with the Discord bot.
        smart: When True, use rewriting+reranking pipeline. Adds ~2 Haiku
            calls of latency in exchange for higher hit rate.
    """
    try:
        user_id = _validated_user_id(user_id)
    except ValueError as e:
        return f"(rejected: {e})"
    limit = _clamped_limit(limit)
    if smart:
        memories = await search_memories_smart(
            query=query, user_id=user_id, limit=limit,
        )
    else:
        memories = await search_memories(query=query, user_id=user_id, limit=limit)
    if not memories:
        return f"(no memories found for query: {query!r})"
    return _format_for_mcp(memories)


@mcp.tool()
async def add(
    user_message: str,
    assistant_message: str,
    user_id: str = DEFAULT_USER_ID,
    context: str = "",
) -> str:
    """Store a conversation turn so Mem0 can extract new long-term facts.

    Mem0's LLM extractor (Claude Haiku via OAuth) will distill durable
    facts from the exchange and store them keyed by user_id.

    For richer extraction, pass recent conversation context (e.g. the
    last few messages in the thread) via the `context` parameter. This
    helps Mem0 pick up facts that only make sense in context.

    Args:
        user_message: The user's message text.
        assistant_message: The assistant's reply text.
        user_id: Mem0 user key (default: 先輩's Discord ID).
        context: Optional recent conversation context to prepend.
            Include the last few messages leading up to this exchange
            so Mem0's extractor can understand the full conversational flow.
    """
    try:
        user_id = _validated_user_id(user_id)
    except ValueError as e:
        return f"(rejected: {e})"
    msgs: list[dict[str, str]] = []
    if context:
        msgs.append({
            "role": "system",
            "content": (
                "[Recent conversation context]\n"
                + context
                + "\n[End of context]"
            ),
        })
    msgs.append({"role": "user", "content": user_message})
    msgs.append({"role": "assistant", "content": assistant_message})
    result = await add_memories(msgs, user_id=user_id)
    if not result:
        return "(mem0 add failed — see server logs)"
    count = len(result.get("results", [])) if isinstance(result, dict) else 0
    return f"stored {count} new memories for user={user_id}"


@mcp.tool()
async def list_all(user_id: str = DEFAULT_USER_ID, limit: int = 50) -> str:
    """List memories Mem0 holds for the given user (truncated to `limit`).

    Use `search` for targeted lookups — list_all is for sampling/overview.

    Args:
        user_id: Mem0 user key (default: 先輩's Discord ID).
        limit: Max number of memories to return (default 50). The full
            count is huge; raise this only when you really need it.
    """
    try:
        user_id = _validated_user_id(user_id)
    except ValueError as e:
        return f"(rejected: {e})"
    limit = _clamped_limit(limit)
    memories = await get_all_memories(user_id=user_id)
    if not memories:
        return f"(no memories stored for user={user_id})"
    return _format_for_mcp(memories, limit=limit)


@mcp.tool()
async def delete(memory_id: str) -> str:
    """Delete a specific memory by its Mem0 ID.

    Args:
        memory_id: The memory ID returned by `list_all` or `search`.
    """
    ok = await delete_memory(memory_id)
    return f"deleted {memory_id}" if ok else f"delete failed for {memory_id}"


if __name__ == "__main__":
    log.info("Starting mem0 MCP server (default user_id=%s)", DEFAULT_USER_ID)
    mcp.run()

"""MCP server wrapping the Mem0 long-term memory store used by the Discord bot.

Exposes Mem0 search/add/list/delete as MCP tools so Claude Code can read and
write the same ChromaDB that かなえ Discord bot uses. Default user_id is
hardcoded to 先輩's Discord ID so記憶がそのまま共有される。

Reuses discord-bot's services.mem0_service which handles OAuth + config.
"""

import asyncio
import logging
import os
import sys

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
    format_memories_for_prompt,
    get_all_memories,
    search_memories,
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

mcp = FastMCP("mem0")


@mcp.tool()
async def search(query: str, limit: int = 5, user_id: str = DEFAULT_USER_ID) -> str:
    """Semantic search against the shared Mem0 long-term memory.

    Returns a formatted block of relevant memories for the given query.
    Use this when you want to recall facts about 先輩 (preferences,
    past decisions, ongoing projects, personal details).

    Args:
        query: Natural language query describing what you want to recall.
        limit: Max number of memories to return (default 5).
        user_id: Mem0 user key. Defaults to 先輩's Discord ID so results
            are shared with the Discord bot.
    """
    memories = await search_memories(query=query, user_id=user_id, limit=limit)
    if not memories:
        return f"(no memories found for query: {query!r})"
    return format_memories_for_prompt(memories)


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
async def list_all(user_id: str = DEFAULT_USER_ID) -> str:
    """List every memory Mem0 holds for the given user.

    Args:
        user_id: Mem0 user key (default: 先輩's Discord ID).
    """
    memories = await get_all_memories(user_id=user_id)
    if not memories:
        return f"(no memories stored for user={user_id})"
    return format_memories_for_prompt(memories)


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

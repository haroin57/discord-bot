"""MCP server wrapping the Mem0 long-term memory store used by the Discord bot.

Exposes Mem0 search/add/list/delete as MCP tools so Claude Code can read and
write the same ChromaDB that かなえ Discord bot uses. Default user_id is
hardcoded to 先輩's Discord ID so記憶がそのまま共有される。

Reuses discord-bot's services.mem0 which handles OAuth + config.
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

# Don't refresh OAuth in this subprocess. The parent (Discord bot) owns
# the refresh schedule; if both ends refresh independently, the child's
# refresh invalidates the parent's in-flight access token and causes 401
# Unauthorized on the next API call. We re-read from disk via
# ensure_valid_token() to pick up tokens rotated by the parent.
# MUST be set before any `services.oauth_manager` import.
os.environ["OAUTH_NO_REFRESH"] = "1"

from mcp.server.fastmcp import FastMCP  # noqa: E402

from services.mem0 import (  # noqa: E402
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


def _format_for_mcp(memories: list[dict], limit: int | None = None, *, details: bool = False) -> str:
    """MCP-safe formatter without bracketed boundary markers.

    The previous formatter used "[Mem0 — ...]" / "[End of Mem0 memories]"
    which Claude's CLI sometimes interpreted as a stop boundary, causing
    the assistant to halt after receiving the tool result.
    """
    from services.mem0.format import _scrub_fact_text, _fact_meta_suffix
    lines = []
    for m in memories:
        text = m.get("memory", m.get("text", ""))
        if text:
            suffix = _fact_meta_suffix(m) if details else ""
            if details and m.get("id"):
                suffix += f" [id={_scrub_fact_text(str(m['id'])[:120])}]"
            if details:
                metadata = m.get("metadata") or {}
                if isinstance(metadata, dict):
                    for key in ("source", "source_timestamp", "source_message_ids", "source_quote", "derived_from"):
                        if metadata.get(key):
                            value = str(metadata[key])
                            value = value[:400] + ("…" if len(value) > 400 else "")
                            suffix += f" [{key}={_scrub_fact_text(value).replace(chr(10), ' ')}]"
            lines.append(f"- {_scrub_fact_text(str(text))}{suffix}")
    if limit is not None and len(lines) > limit:
        truncated = len(lines) - limit
        lines = lines[:limit]
        lines.append(f"(+{truncated} more — narrow your query to see them)")
    return "\n".join(lines) if lines else ""


mcp = FastMCP("mem0")


@mcp.tool()
async def search(
    query: str, limit: int = 5,
    user_id: str = DEFAULT_USER_ID, smart: bool = False,
    as_of: str = "", channel_id: str = "", details: bool = False,
) -> str:
    """Semantic search against the shared Mem0 long-term memory.

    Returns a formatted block of relevant memories for the given query.
    Use this when you want to recall facts about 先輩 (preferences,
    past decisions, ongoing projects, personal details).

    Plain vector search is the default. Pass `smart=True` for the higher-cost
    pipeline that (1) rewrites the query into canonical-keyword variants via
    Haiku, (2) runs multi-query parallel search, and (3) LLM-reranks the
    merged candidates. Smart mode adds ~1-3 s of latency from two extra Haiku
    round-trips; enable it only when default search misses what you need.

    Each returned memory carries a `metadata` dict. Notable fields:
      - category / subcategory / tags : taxonomy from extract.py
      - importance (1-5) : reinforced by +1 on each duplicate hit
      - tier (0=Live, 1=Hot, 2=Warm, 3=Cold) : weekly_compact decay state
      - mention_count : Hebbian counter, bumped when a duplicate is ingested
      - reinforced_at (list[int]) / last_reinforced_at : reinforcement history
      - value_class : noise/transient/preference/decision/knowledge/episodic
      - created_at / created_at_unix : ingest time (when present)

    Args:
        query: Natural language query describing what you want to recall.
        limit: Max number of memories to return (default 5).
        user_id: Mem0 user key. Defaults to 先輩's Discord ID so results
            are shared with the Discord bot.
        smart: When True, use rewriting+reranking pipeline. Adds ~2 Haiku
            calls of latency in exchange for higher hit rate. Default: False.
        as_of: Optional timezone-aware ISO8601 time for alpha historical/future
            validity search. This explicit hybrid mode takes precedence over smart.
        channel_id: Optional conversation scope, without changing user ownership.
        details: Include memory IDs and available condition/time/source metadata.
    """
    try:
        user_id = _validated_user_id(user_id)
    except ValueError as e:
        return f"(rejected: {e})"
    limit = _clamped_limit(limit)
    scope_kwargs = {"current_channel_id": channel_id} if channel_id else {}
    if as_of:
        from services.mem0 import search as search_module
        temporal_search = getattr(search_module, "search_memories_at_time", None)
        if temporal_search is None:
            return "(rejected: time-specific search requires Mem0 alpha)"
        try:
            memories = await temporal_search(query, user_id, as_of, limit=limit, **scope_kwargs)
        except ValueError as exc:
            return f"(rejected: {exc})"
    elif smart:
        memories = await search_memories_smart(
            query=query, user_id=user_id, limit=limit, **scope_kwargs,
        )
    else:
        memories = await search_memories(query=query, user_id=user_id, limit=limit, **scope_kwargs)
    if not memories:
        return f"(no memories found for query: {query!r})"
    return _format_for_mcp(memories, details=details)


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
        return "(mem0 add failed — pipeline unavailable, e.g. ChromaDB down or init cooldown)"
    if isinstance(result, dict) and result.get("error"):
        return f"(mem0 add failed: {result['error']})"
    count = len(result.get("results", [])) if isinstance(result, dict) else 0
    return f"stored {count} new memories for user={user_id}"


@mcp.tool()
async def list_all(user_id: str = DEFAULT_USER_ID, limit: int = 50) -> str:
    """List memories Mem0 holds for the given user (truncated to `limit`).

    Use `search` for targeted lookups — list_all is for sampling/overview.

    Each memory's `metadata` dict has the same shape as `search` returns
    (category, importance, tier, mention_count, reinforced_at, value_class,
    created_at_unix, …). See the `search` docstring for definitions.

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
    # Pass `limit` down to ChromaDB instead of fetching the full collection
    # and slicing in Python. With ~860+ memories the prior behavior allocated
    # the full list before discarding most of it; this stops the underlying
    # query early.
    memories = await get_all_memories(user_id=user_id, limit=limit)
    if not memories:
        return f"(no memories stored for user={user_id})"
    return _format_for_mcp(memories, limit=limit)


@mcp.tool()
async def delete(memory_id: str, user_id: str = DEFAULT_USER_ID) -> str:
    """Delete a specific memory by its Mem0 ID.

    Only the owner of the memory (matched by user_id) can delete it.

    Args:
        memory_id: The memory ID returned by `list_all` or `search`.
        user_id: Discord snowflake of the requesting user. Must match the
            user_id stored on the memory record.
    """
    try:
        user_id = _validated_user_id(user_id)
    except ValueError as e:
        return f"(rejected: {e})"

    # Verify ownership before deletion by searching all memories for this user
    # and confirming the target memory_id exists in their namespace.
    user_memories = await get_all_memories(user_id=user_id)
    owned_ids = {m.get("id") for m in user_memories if m.get("id")}
    if memory_id not in owned_ids:
        return "permission denied"

    ok = await delete_memory(memory_id)
    return f"deleted {memory_id}" if ok else f"delete failed for {memory_id}"


if __name__ == "__main__":
    log.info("Starting mem0 MCP server (default user_id=%s)", DEFAULT_USER_ID)
    # Shared-instance mode: when MCP_HTTP_PORT is set, serve one streamable-HTTP
    # daemon that every parallel Claude Code session shares (see systemd unit
    # mcp-mem0-shared.service). When unset, fall back to stdio so the Discord
    # bot's on-demand spawn path and `claude mcp add ... -- python …` keep
    # working unchanged. MUST remain backward compatible.
    _http_port = os.environ.get("MCP_HTTP_PORT")
    if _http_port:
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = int(_http_port)
        log.info("mem0 MCP serving streamable-http on 127.0.0.1:%s/mcp", _http_port)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()

"""MCP server wrapper for llm_mem0 (Mem0α).

Exposes the library's public memory API as Model Context Protocol tools so
any MCP client (Claude Code, Claude Desktop, Cursor, ...) can use Mem0α as a
long-term memory backend without writing Python.

Run::

    pip install "mem0-alpha[mcp]"
    mem0-alpha-mcp                    # stdio transport

Claude Code / Claude Desktop config example::

    {
      "mcpServers": {
        "mem0-alpha": {
          "command": "mem0-alpha-mcp",
          "env": {"MEM0_DEFAULT_USER_ID": "you"}
        }
      }
    }

Configuration (vector store address, models, auth backend) is inherited from
``llm_mem0.settings`` env vars — see the README's Configuration section.
``MEM0_DEFAULT_USER_ID`` makes the ``user_id`` argument optional on every
tool, which keeps single-user client configs to zero per-call arguments.

The ``mcp`` package is an optional dependency; this module is the only place
that imports it, so the core library keeps working without it.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging

log = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover - exercised only without extras
    raise ImportError(
        "MCP support requires the optional dependency: "
        "pip install 'mem0-alpha[mcp]'"
    ) from e

from llm_mem0 import (
    add_memories,
    delete_memory as _delete_memory,
    format_memories_for_prompt,
    get_all_memories,
    search_memories,
    search_memories_smart,
)
from llm_mem0.settings import settings

mcp = FastMCP("mem0-alpha")


class _MissingUserId(ValueError):
    pass


def _resolve_user_id(user_id: str) -> str:
    """Explicit argument wins; fall back to MEM0_DEFAULT_USER_ID."""
    uid = (user_id or "").strip() or settings.MEM0_DEFAULT_USER_ID
    if not uid:
        raise _MissingUserId(
            "user_id is required: pass it as an argument or set the "
            "MEM0_DEFAULT_USER_ID env var in the MCP server config."
        )
    return uid


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def _safe_details(value):
    from .format import _scrub_fact_text
    if isinstance(value, str):
        return html.escape(_scrub_fact_text(value[:4000]), quote=False)
    if isinstance(value, dict):
        return {str(_safe_details(k)): _safe_details(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_details(v) for v in value]
    return value


@mcp.tool()
async def add_memory(
    user_text: str,
    user_id: str = "",
    assistant_text: str = "",
    context_hint: str = "",
) -> str:
    """Store a conversation turn as long-term memory facts.

    Runs the extraction gate (one small-model call) that turns the turn into
    clean, deduplicated facts about the user before storing them.

    Args:
        user_text: What the user said.
        user_id: Stable ID of the user the facts belong to (falls back to
            the MEM0_DEFAULT_USER_ID env var).
        assistant_text: The assistant reply, for context (optional).
        context_hint: Extra context for the extractor (optional).
    """
    try:
        uid = _resolve_user_id(user_id)
    except _MissingUserId as exc:
        return f"Error: {exc}"
    result = await add_memories(
        user_text=user_text,
        assistant_text=assistant_text,
        user_id=uid,
        context_hint=context_hint,
    )
    if result is None:
        return (
            "Error: memory store unavailable (is the ChromaDB server "
            "running and reachable?)."
        )
    if isinstance(result, dict):
        if result.get("error"):
            return f"Error: {result['error']}"
        if result.get("status") in {"error", "partial"} or result.get("failed"):
            return "Error: ingestion incomplete; retry with the same source identifiers."
        n = len(result.get("results") or [])
        if n:
            skipped = result.get("skipped") or {}
            dup = skipped.get("near_dup", 0)
            suffix = f" ({dup} duplicate(s) merged/skipped)" if dup else ""
            return f"Stored {n} fact(s).{suffix}"
        return "Processed (no new facts extracted or all were duplicates)."
    return "Stored."


@mcp.tool()
async def search_memory(
    query: str,
    user_id: str = "",
    limit: int = 5,
    smart: bool = False,
    channel_id: str | None = None,
    as_of: int | str | None = None,
    details: bool = False,
) -> str:
    """Search long-term memories; returns a prompt-ready, injection-safe block.

    Args:
        query: Natural-language search query.
        user_id: Whose memories to search (falls back to MEM0_DEFAULT_USER_ID).
        limit: Maximum number of memories to return.
        smart: Enable query-rewrite + LLM rerank (slower, higher recall).
        channel_id: Conversation scope to include.
        as_of: Unix seconds or ISO8601 with timezone, for historical validity.
        details: Include IDs, provenance and retrieval evidence as untrusted JSON data.
    """
    try:
        uid = _resolve_user_id(user_id)
        limit = _limit(limit)
    except ValueError as exc:
        return f"Error: {exc}"
    kwargs = {"limit": limit}
    if channel_id is not None:
        kwargs["current_channel_id"] = channel_id
    try:
        from .client import _get_mem0
        if await asyncio.to_thread(_get_mem0) is None:
            return "Error: memory store unavailable."
        if as_of is not None:
            from .search import search_memories_at_time
            memories = await search_memories_at_time(query, uid, as_of, **kwargs)
        elif smart:
            memories = await search_memories_smart(query, uid, **kwargs)
        else:
            memories = await search_memories(query, uid, **kwargs)
    except (ValueError, RuntimeError):
        return "Error: search failed; check the store and temporal parameters."
    if details:
        return json.dumps({"untrusted_memory_data": _safe_details(memories)}, ensure_ascii=False)
    if not memories:
        return "(no relevant memories)"
    return format_memories_for_prompt(memories)


@mcp.tool()
async def list_memories(
    user_id: str = "",
    limit: int = 50,
    offset: int = 0,
    details: bool = False,
) -> str:
    """List stored memories with their IDs, for review and housekeeping.

    Returns one line per memory: ``<id>  [imp=N tier=N]  <text>``. Use the
    id with the delete_memory tool.

    Args:
        user_id: Whose memories to list (falls back to MEM0_DEFAULT_USER_ID).
        limit: Maximum number of rows to return.
        offset: Number of rows to skip (0..10000); enumeration is not a snapshot.
        details: Include complete metadata as untrusted JSON data.
    """
    try:
        uid = _resolve_user_id(user_id)
        limit = _limit(limit)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10000:
            raise ValueError("offset must be between 0 and 10000")
    except ValueError as exc:
        return f"Error: {exc}"
    try:
        rows = (await get_all_memories(uid, limit=limit + offset, strict=True))[offset:]
    except Exception:
        return "Error: memory store unavailable or enumeration failed."
    if details:
        return json.dumps({"untrusted_memory_data": _safe_details(rows), "offset": offset}, ensure_ascii=False)
    if not rows:
        return "(no memories stored for this user)"
    lines = []
    for r in rows:
        md = r.get("metadata") or {}
        meta_bits = []
        if md.get("importance") is not None:
            meta_bits.append(f"imp={md.get('importance')}")
        if md.get("tier") is not None:
            meta_bits.append(f"tier={md.get('tier')}")
        if md.get("valid_to"):
            meta_bits.append("archived")
        meta = f" [{' '.join(meta_bits)}]" if meta_bits else ""
        text = (r.get("memory") or r.get("text") or "").replace("\n", " ")
        lines.append(_safe_details(f"{r.get('id', '?')}{meta}  {text[:200]}"))
    return "\n".join(lines)


@mcp.tool()
async def delete_memory(memory_id: str) -> str:
    """Delete one memory by ID (see list_memories for IDs).

    Also removes the memory's rows from the side indices (entity-graph
    edges and the BM25 keyword index) so hybrid retrieval can't resurface
    the deleted fact.

    Args:
        memory_id: The memory's ID as shown by list_memories.
    """
    mid = (memory_id or "").strip()
    if not mid:
        return "Error: memory_id is required."
    ok = await _delete_memory(mid)
    if not ok:
        return f"Error: failed to delete {mid} (wrong id, or store unavailable)."
    return f"Deleted {_safe_details(mid)}."


@mcp.tool()
async def fact_history(attribute: str, user_id: str = "", limit: int = 50) -> str:
    """Inspect a user's derived attribute correction links and validity times.

    Body copies are truncated; IDs refer to the authoritative memory store.
    """
    try:
        uid = _resolve_user_id(user_id)
        limit = _limit(limit)
    except ValueError as exc:
        return f"Error: {exc}"
    from .fact_store import get_history
    rows = await asyncio.to_thread(get_history, uid, attribute, limit)
    return json.dumps({"untrusted_fact_history": _safe_details(rows)}, ensure_ascii=False)


def main() -> None:
    """Entry point for the ``mem0-alpha-mcp`` console script (stdio)."""
    # stdio transport: stdout is the protocol channel, so route our logs to
    # stderr explicitly (FastMCP does the same for its own logs).
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()

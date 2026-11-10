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
import logging

from pydantic import StrictFloat, StrictInt, StrictStr

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
from . import api_contracts as contracts

mcp = FastMCP("mem0-alpha")


class _MissingUserId(ValueError):
    pass


def _resolve_user_id(user_id: str) -> str:
    """Explicit argument wins; fall back to MEM0_DEFAULT_USER_ID."""
    uid = contracts.identifier(user_id, "user_id") or settings.MEM0_DEFAULT_USER_ID
    if not uid:
        raise _MissingUserId(
            "user_id is required: pass it as an argument or set the "
            "MEM0_DEFAULT_USER_ID env var in the MCP server config."
        )
    return contracts.identifier(uid, "user_id", required=True)


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def _safe_details(value):
    from .format import _scrub_fact_text
    return contracts.bounded_details(value, lambda text: html.escape(_scrub_fact_text(text), quote=False))


@mcp.tool()
async def add_memory(
    user_text: str,
    user_id: str = "",
    assistant_text: str = "",
    context_hint: str = "",
    request_id: str | None = None,
    source_id: str | None = None,
    source_message_ids: list[str] | None = None,
    source_author_id: str | None = None,
    source_timestamp: StrictInt | StrictFloat | StrictStr | None = None,
    channel_id: str | None = None,
    valid_from: StrictInt | StrictFloat | StrictStr | None = None,
    valid_to: StrictInt | StrictFloat | StrictStr | None = None,
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
        request_id: Stable caller event ID; reuse for retries of the same input.
        source_id: Explicit source reference, not an authorization identity.
        source_message_ids: Up to 100 source references of at most 256 characters.
        source_author_id: Provenance only; never changes the memory owner.
        source_timestamp: Original observation time, Unix seconds or timezone-aware ISO8601.
        channel_id: Original conversation scope identifier.
        valid_from: Explicit effective start time, Unix seconds or timezone-aware ISO8601.
        valid_to: Explicit effective end time, not earlier than valid_from.
    """
    try:
        uid = _resolve_user_id(user_id)
        contracts.validate_input_text(user_text=user_text, assistant_text=assistant_text, context_hint=context_hint)
        metadata = contracts.provenance_metadata(request_id=request_id, source_id=source_id,
            source_message_ids=source_message_ids, source_author_id=source_author_id,
            source_timestamp=source_timestamp, channel_id=channel_id, valid_from=valid_from, valid_to=valid_to)
    except ValueError as exc:
        return f"Error: {exc}"
    try:
        result = await add_memories(user_text=user_text, assistant_text=assistant_text,
            user_id=uid, context_hint=context_hint, **({"metadata": metadata} if metadata else {}))
    except Exception:
        return "Error: ingestion failed; verify the result and retry with the same request_id."
    outcome = contracts.classify_add_outcome(result)
    if outcome.status != "ok":
        return "Error: " + contracts.add_failure(outcome, result, request_id)
    if not outcome.stored:
        return "Processed (no new facts extracted or all were duplicates)."
    skipped = result.get("skipped") or {}
    duplicates = skipped.get("near_dup", 0) if isinstance(skipped, dict) else 0
    suffix = f" ({duplicates} duplicate(s) merged/skipped)" if type(duplicates) is int and duplicates > 0 else ""
    return f"Stored {outcome.stored} fact(s).{suffix}"


@mcp.tool()
async def search_memory(
    query: str,
    user_id: str = "",
    limit: StrictInt = 5,
    smart: bool = False,
    channel_id: str | None = None,
    as_of: StrictInt | StrictStr | None = None,
    details: bool = False,
    mode: str | None = None,
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
        mode: Explicit vector/hybrid/smart. Omitted preserves smart/default behavior; as_of takes precedence.
    """
    try:
        uid = _resolve_user_id(user_id)
        limit = _limit(limit)
        contracts.validate_input_text(query=query)
        selected_mode = contracts.search_mode(mode, smart)
        if channel_id is not None:
            channel_id = contracts.identifier(channel_id, "channel_id")
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
        elif selected_mode == "hybrid":
            from .search import search_memories_hybrid
            memories = await search_memories_hybrid(query, uid, **kwargs)
        elif selected_mode == "smart":
            memories = await search_memories_smart(query, uid, **kwargs)
        else:
            memories = await search_memories(query, uid, **kwargs)
    except Exception:
        return "Error: search failed; check the store and temporal parameters."
    if details:
        return contracts.details_json("untrusted_memory_data", memories, _safe_details)
    if not memories:
        return "(no relevant memories)"
    return contracts.bound_output(format_memories_for_prompt(memories))


@mcp.tool()
async def list_memories(
    user_id: str = "",
    limit: StrictInt = 50,
    offset: StrictInt = 0,
    details: bool = False,
) -> str:
    """List stored memories with their IDs, for review and housekeeping.

    Returns one line per memory: ``<id>  [imp=N tier=N]  <text>``. Use the
    id with the delete_memory tool.

    Args:
        user_id: Whose memories to list (falls back to MEM0_DEFAULT_USER_ID).
        limit: Maximum number of rows to return.
        offset: Number of rows to skip (0..10000); enumeration is not a snapshot.
        details: Include bounded metadata as untrusted JSON data.
    """
    try:
        uid = _resolve_user_id(user_id)
        limit = _limit(limit)
        offset = contracts.page_offset(offset)
    except ValueError as exc:
        return f"Error: {exc}"
    try:
        rows = (await get_all_memories(uid, limit=limit + offset, strict=True))[offset:offset + limit]
    except Exception:
        return "Error: memory store unavailable or enumeration failed."
    if details:
        return contracts.details_json("untrusted_memory_data", rows, _safe_details, offset=offset)
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
    return contracts.bound_output("\n".join(lines))


@mcp.tool()
async def delete_memory(memory_id: str) -> str:
    """Delete one memory by ID (see list_memories for IDs).

    Also removes the memory's rows from the side indices (entity-graph
    edges and the BM25 keyword index) so hybrid retrieval can't resurface
    the deleted fact.

    Args:
        memory_id: The memory's ID as shown by list_memories.
    """
    try:
        mid = contracts.identifier(memory_id, "memory_id", required=True)
        ok = await _delete_memory(mid)
    except ValueError:
        return "Error: valid memory_id is required."
    except Exception:
        return "Error: deletion failed; verify the store before retrying."
    if not ok:
        return "Error: failed to delete memory (wrong id, or store unavailable)."
    return f"Deleted {_safe_details(mid)}."


@mcp.tool()
async def fact_history(attribute: str, user_id: str = "", limit: StrictInt = 50) -> str:
    """Inspect a user's derived attribute correction links and validity times.

    Body copies are truncated; IDs refer to the authoritative memory store.
    """
    try:
        uid = _resolve_user_id(user_id)
        limit = _limit(limit)
        attribute = contracts.identifier(attribute, "attribute", required=True)
    except ValueError as exc:
        return f"Error: {exc}"
    from .fact_store import get_history
    if not settings.MEM0_FACT_STORE_ENABLED:
        return "Error: fact history is disabled; absence of lineage is not absence of memories."
    try:
        rows = await asyncio.to_thread(get_history, uid, attribute, limit, strict=True)
    except Exception:
        return "Error: fact history store unavailable."
    return contracts.details_json("untrusted_fact_history", rows, _safe_details)


def main() -> None:
    """Entry point for the ``mem0-alpha-mcp`` console script (stdio)."""
    # stdio transport: stdout is the protocol channel, so route our logs to
    # stderr explicitly (FastMCP does the same for its own logs).
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()

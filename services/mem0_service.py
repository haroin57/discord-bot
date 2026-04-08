"""Mem0 memory service — semantic long-term memory for the Discord bot.

ChromaDB (local vector store) + Anthropic Claude Haiku via OAuth (memory extraction)
+ OpenAI text-embedding-3-small (embeddings).

Authentication: Uses Claude Code OAuth tokens (~/.claude/.credentials.json)
instead of a standalone ANTHROPIC_API_KEY. The oauth_manager patches the
Anthropic SDK globally to use Bearer auth.
"""

import asyncio
import logging
import os

from services.oauth_manager import ensure_valid_token, init as oauth_init

log = logging.getLogger(__name__)

_mem0_instance = None
_init_attempted = False


def _build_config() -> dict:
    """Build Mem0 configuration from env vars."""
    from config import STATE_DIR

    db_path = os.path.join(STATE_DIR, "mem0_db")
    os.makedirs(db_path, exist_ok=True)

    config: dict = {
        "llm": {
            "provider": os.environ.get("MEM0_LLM_PROVIDER", "anthropic"),
            "config": {
                "model": os.environ.get("MEM0_LLM_MODEL", "claude-haiku-4-5-20251001"),
                "temperature": 0.1,
                "max_tokens": 2000,
                # api_key intentionally omitted — OAuth via ANTHROPIC_AUTH_TOKEN
            },
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "kanae_memories",
                "path": db_path,
            },
        },
        "version": "v1.1",
    }

    # Embedder — defaults to OpenAI text-embedding-3-small
    embedder_provider = os.environ.get("MEM0_EMBEDDER_PROVIDER", "openai")
    embedder_model = os.environ.get("MEM0_EMBEDDER_MODEL", "text-embedding-3-small")
    config["embedder"] = {
        "provider": embedder_provider,
        "config": {
            "model": embedder_model,
        },
    }

    return config


def _get_mem0():
    """Lazy-init singleton Mem0 instance."""
    global _mem0_instance, _init_attempted

    if _init_attempted:
        return _mem0_instance
    _init_attempted = True

    # Initialize OAuth BEFORE creating any Anthropic clients
    if not oauth_init():
        log.error("OAuth init failed — Mem0 LLM calls will not work")

    try:
        from mem0 import Memory

        cfg = _build_config()
        _mem0_instance = Memory.from_config(cfg)
        log.info(
            "Mem0 ready — llm=%s (OAuth) embedder=%s store=chroma(%s)",
            cfg["llm"]["config"]["model"],
            cfg["embedder"]["config"]["model"],
            cfg["vector_store"]["config"]["path"],
        )
    except Exception as exc:
        log.error("Mem0 init failed: %s", exc, exc_info=True)
        _mem0_instance = None

    return _mem0_instance


# ------------------------------------------------------------------
# Public async API
# ------------------------------------------------------------------

async def search_memories(query: str, user_id: str, limit: int = 5) -> list[dict]:
    """Semantic search for relevant memories."""
    mem = _get_mem0()
    if not mem:
        return []
    try:
        await ensure_valid_token()  # refresh OAuth if needed
        raw = await asyncio.to_thread(mem.search, query=query, user_id=user_id, limit=limit)
        results = raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        return results
    except Exception as exc:
        log.warning("Mem0 search error: %s", exc)
        return []


async def add_memories(
    messages: list[dict], user_id: str, metadata: dict | None = None
) -> dict | None:
    """Store conversation for automatic fact extraction."""
    mem = _get_mem0()
    if not mem:
        return None
    try:
        await ensure_valid_token()  # refresh OAuth if needed
        result = await asyncio.to_thread(
            mem.add, messages, user_id=user_id, metadata=metadata or {}
        )
        count = len(result.get("results", [])) if isinstance(result, dict) else 0
        if count:
            log.info("Mem0: extracted %d memories for user=%s", count, user_id)
        return result
    except Exception as exc:
        log.warning("Mem0 add error: %s", exc)
        return None


async def get_all_memories(user_id: str) -> list[dict]:
    """Retrieve all memories for a user."""
    mem = _get_mem0()
    if not mem:
        return []
    try:
        await ensure_valid_token()  # refresh OAuth if needed
        raw = await asyncio.to_thread(mem.get_all, user_id=user_id)
        return raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    except Exception as exc:
        log.warning("Mem0 get_all error: %s", exc)
        return []


async def delete_memory(memory_id: str) -> bool:
    """Delete a specific memory by ID."""
    mem = _get_mem0()
    if not mem:
        return False
    try:
        await ensure_valid_token()  # refresh OAuth if needed
        await asyncio.to_thread(mem.delete, memory_id)
        return True
    except Exception as exc:
        log.warning("Mem0 delete error: %s", exc)
        return False


# ------------------------------------------------------------------
# Prompt formatting
# ------------------------------------------------------------------

def format_memories_for_prompt(memories: list[dict]) -> str:
    """Format retrieved memories as a prompt-injection block."""
    if not memories:
        return ""

    lines = ["[Mem0 — この人についての長期記憶]"]
    for m in memories:
        text = m.get("memory", m.get("text", ""))
        if text:
            score = m.get("score")
            if score is not None:
                lines.append(f"- {text} (relevance: {score:.2f})")
            else:
                lines.append(f"- {text}")
    lines.append("[End of Mem0 memories]")
    return "\n".join(lines)

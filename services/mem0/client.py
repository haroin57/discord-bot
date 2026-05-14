"""Mem0 singleton + OAuth token refresh + low-level housekeeping (get_all/delete)."""

import asyncio
import logging
import os

from services.oauth_manager import ensure_valid_token, get_access_token, init as oauth_init

log = logging.getLogger(__name__)

# Mem0's Chroma adapter returns ``score = distance`` (small = similar; see
# services/mem0/dedup.py header). Filter out anything past this distance —
# rerank handles fine-grained quality, so this only needs to drop obvious
# garbage, not borderline matches.
MEM0_RELEVANCE_MAX_DISTANCE = 1.0
# Back-compat alias for any caller still importing the old name. DO NOT use
# in new code — the semantic flipped from "min similarity" to "max distance"
# on 2026-05-14 when we discovered the codebase was filtering backwards.
MEM0_RELEVANCE_THRESHOLD = MEM0_RELEVANCE_MAX_DISTANCE

_mem0_instance = None
_init_attempted = False
_last_token: str | None = None

# Shared AsyncAnthropic client for Haiku-backed extraction / rewrite / rerank
# calls. Re-created only when the OAuth token actually rotates, so the
# underlying httpx pool / TLS session is reused across all three call sites
# (services/mem0/extract.py, services/mem0/search.py).
_async_anthropic_client = None
_async_anthropic_token: str | None = None
# Serializes (rare) singleton (re)construction so two concurrent callers from
# asyncio.gather cannot both race past the None check and each build a fresh
# AsyncAnthropic. Once initialized the lock is only contended on token
# rotation, which happens at most every ~hour — negligible overhead.
_async_anthropic_lock = asyncio.Lock()


async def get_anthropic_client():
    """Return a singleton AsyncAnthropic client, refreshing only on token change.

    Returns None if the OAuth token cannot be obtained — callers must handle
    that as a soft failure and fall back to their best-effort behavior.
    """
    global _async_anthropic_client, _async_anthropic_token
    oauth_init()  # idempotent
    await ensure_valid_token()
    token = get_access_token()
    if not token:
        return None
    # Fast path: no rebuild needed.
    if _async_anthropic_client is not None and token == _async_anthropic_token:
        return _async_anthropic_client
    # Slow path: build (or rebuild after token rotation) under a lock so two
    # concurrent callers cannot both construct a fresh client.
    async with _async_anthropic_lock:
        # Re-check after acquiring the lock — another coroutine may have
        # already built the right client while we were waiting.
        if _async_anthropic_client is None or token != _async_anthropic_token:
            from anthropic import AsyncAnthropic
            # max_retries=0 — Mem0 ingest treats any 429 as fail-open, and
            # the SDK's default 5x retry on rate-limit-error turns one burst
            # into 5 wasted calls, ratcheting the per-minute limiter for the
            # next caller. Bumped from default 2 to 0 on 2026-05-14 after
            # the channel-history ingest saturated the account.
            _async_anthropic_client = AsyncAnthropic(
                auth_token=token, max_retries=0,
            )
            _async_anthropic_token = token
    return _async_anthropic_client


def _refresh_mem0_client_token() -> None:
    """Update the auth_token on Mem0's internal Anthropic client after OAuth refresh."""
    global _last_token
    current = get_access_token()
    if current and current != _last_token and _mem0_instance is not None:
        try:
            # Mem0 stores the Anthropic client at .llm.client
            llm = getattr(_mem0_instance, "llm", None)
            client = getattr(llm, "client", None) if llm else None
            if client and hasattr(client, "auth_token"):
                client.auth_token = current
                _last_token = current
                log.debug("Refreshed Mem0 Anthropic client token")
        except Exception as e:
            log.debug("Failed to refresh Mem0 client token: %s", e)


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


# ---------------------------------------------------------------------------
# Low-level housekeeping (direct Mem0 access — used by weekly_mem0_compact.py)
# ---------------------------------------------------------------------------

async def get_all_memories(user_id: str, limit: int | None = None) -> list[dict]:
    """Retrieve memories for a user.

    `limit` is forwarded to Mem0 / ChromaDB so the underlying query can stop
    early instead of fetching the entire collection and slicing in Python.
    Pass None (default) to fetch all (used by the weekly housekeeping cron;
    avoid on hot paths).
    """
    mem = _get_mem0()
    if not mem:
        return []
    try:
        await ensure_valid_token()  # refresh OAuth if needed
        _refresh_mem0_client_token()
        kwargs: dict = {"user_id": user_id}
        if limit is not None:
            kwargs["limit"] = limit
        raw = await asyncio.to_thread(mem.get_all, **kwargs)
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
        _refresh_mem0_client_token()
        await asyncio.to_thread(mem.delete, memory_id)
        return True
    except Exception as exc:
        log.warning("Mem0 delete error: %s", exc)
        return False

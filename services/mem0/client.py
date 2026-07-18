"""Mem0 singleton + OAuth token refresh + low-level housekeeping (get_all/delete)."""

import asyncio
import logging
import os
import time

from services.oauth_manager import (
    ensure_valid_token,
    get_access_token,
    init as oauth_init,
)

log = logging.getLogger(__name__)

# Mem0's Chroma adapter returns ``score = distance`` (small = similar; see
# services/mem0/dedup.py header).
# Pre-rerank gate: applied inside search_memories() to drop candidates
# that are physically too far before the LLM rerank ever sees them. Also
# used as the sanity gate for the rerank=False path in
# search_memories_smart.
MEM0_RELEVANCE_MAX_DISTANCE = float(
    os.environ.get("MEM0_RELEVANCE_MAX_DISTANCE", "1.2")
)
# DEPRECATED 2026-05-19 (ADR-0002). Previously gated the output of
# search_memories_smart at 0.65 — but cosine distance is a poor judge
# after an LLM rerank has already accepted the fact, and 0.65 was
# pessimistic enough to silently kill every Japanese conversational
# query. search_memories_smart no longer references this on the
# rerank=True path. Kept so env config that sets the var still imports.
MEM0_FINAL_RELEVANCE_MAX_DISTANCE = float(
    os.environ.get("MEM0_FINAL_RELEVANCE_MAX_DISTANCE", "0.65")
)
# Back-compat alias for any caller still importing the old name. DO NOT use
# in new code — the semantic flipped from "min similarity" to "max distance"
# on 2026-05-14 when we discovered the codebase was filtering backwards.
MEM0_RELEVANCE_THRESHOLD = MEM0_RELEVANCE_MAX_DISTANCE

_mem0_instance = None
# Last init failure timestamp (monotonic seconds). When set, _get_mem0()
# returns None without re-trying until _INIT_RETRY_COOLDOWN_SEC has elapsed.
# This replaces the old "_init_attempted = True" latch that permanently
# disabled Mem0 after the first failure — preventing automatic recovery
# when ChromaDB systemd-restarts.
_init_failed_at: float | None = None
_INIT_RETRY_COOLDOWN_SEC = 60.0
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
    """Return a singleton LLM client for extraction/rewrite/rerank.

    Primary: AsyncAnthropic with OAuth token. When the OAuth token is
    unavailable (expired / missing), falls back to an OpenAI-compatible
    client pointed at OpenRouter with DeepSeek V4 Flash.

    Returns None only when neither auth method is available.
    """
    global _async_anthropic_client, _async_anthropic_token
    oauth_init()  # idempotent
    await ensure_valid_token()
    token = get_access_token()

    # Same fallback chain as _build_config / _get_mem0 (P1-e/P4b): both the
    # direct-Haiku path here and Mem0's internal client follow one decision.
    provider = _desired_llm_provider()

    # OAuth token available — use Anthropic directly.
    if provider == "anthropic" and token:
        # Fast path: no rebuild needed.
        if _async_anthropic_client is not None and token == _async_anthropic_token:
            return _async_anthropic_client
        # Slow path: build (or rebuild after token rotation) under a lock.
        async with _async_anthropic_lock:
            if _async_anthropic_client is None or token != _async_anthropic_token:
                from anthropic import AsyncAnthropic

                _async_anthropic_client = AsyncAnthropic(
                    auth_token=token,
                    max_retries=0,
                )
                _async_anthropic_token = token
        return _async_anthropic_client

    # OAuth unavailable — fall back to DeepSeek V4 Flash.
    # Tier 1: OpenRouter (explicit key). Tier 2: OpenCode Zen (free key).
    if provider == "openrouter":
        log.warning(
            "get_anthropic_client: OAuth unavailable, using OpenRouter fallback (%s)",
            _DEEPSEEK_V4_FLASH,
        )
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=_OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            default_headers={"HTTP-Referer": "https://discord-bot.kanae.local"},
        )
    if provider == "opencode":
        log.warning(
            "get_anthropic_client: OAuth unavailable, using OpenCode Zen fallback (%s)",
            _DEEPSEEK_V4_FLASH,
        )
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=_OPENCODE_API_KEY,
            base_url="https://api.opencode.ai/v1",
        )

    # No auth at all (or an explicit non-anthropic MEM0_LLM_PROVIDER override
    # that this direct-call path doesn't serve).
    return None


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


def _has_oauth_token() -> bool:
    """Check whether Anthropic auth is available for Mem0's LLM.

    Uses ``oauth_manager`` as the single source of truth. The old code
    re-parsed ~/.claude/.credentials.json and read a TOP-LEVEL ``expiresAt``,
    but that key is nested under ``claudeAiOauth`` — so it ALWAYS returned
    False and Mem0 silently fell back to DeepSeek/OpenCode even with a valid
    subscription token. oauth_manager.init() loads + validates the token
    (idempotent) and get_access_token() returns it synchronously.
    """
    # Direct API key always works.
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        oauth_init()  # idempotent; populates the cached access token
        return bool(get_access_token())
    except Exception:
        return False


# Fallback LLM config (used when Anthropic OAuth is unavailable).
# Two fallback tiers:
#   1. OPENROUTER_API_KEY → DeepSeek V4 Flash via OpenRouter
#   2. OPENCODE_API_KEY   → DeepSeek V4 Flash via OpenCode Zen API (free tier)
# DeepSeek V4 Flash is fast, cheap, and multilingual. It replaces Haiku for
# Mem0 extraction/rewrite/rerank when the primary Anthropic auth is down.
_OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_OPENCODE_API_KEY = os.environ.get("OPENCODE_API_KEY", "")
_DEEPSEEK_V4_FLASH = "deepseek/deepseek-chat-v4-flash"


def _desired_llm_provider() -> "str | None":
    """Single source of truth for the LLM fallback chain (P1-e).

    Previously this decision lived in two divergent copies — _build_config()
    (Mem0 SDK config) and get_anthropic_client() (direct Haiku calls) — so an
    OAuth failover could flip one path while the other kept its stale
    provider. Order: explicit MEM0_LLM_PROVIDER override → Anthropic (OAuth
    or API key) → OpenRouter → OpenCode Zen → None.
    """
    configured = os.environ.get("MEM0_LLM_PROVIDER", "anthropic")
    if configured != "anthropic":
        return configured  # explicit override wins; no fallback logic
    if _has_oauth_token():
        return "anthropic"
    if _OPENROUTER_API_KEY:
        return "openrouter"
    if _OPENCODE_API_KEY:
        return "opencode"
    return None


def _build_config() -> dict:
    """Build Mem0 configuration from env vars.

    CHROMA_MODE=server (default since 2026-05-16): connect to chromadb HTTP
    server at CHROMA_HOST:CHROMA_PORT. This prevents the multi-process
    write race that corrupted the HNSW index during the SEGV incident.

    CHROMA_MODE=embedded: legacy direct file access (kept for rollback).

    LLM fallback: when Anthropic OAuth is unavailable (expired / missing)
    and OPENROUTER_API_KEY is set, the LLM provider falls back to
    DeepSeek V4 Flash via OpenRouter. This keeps Mem0 functional during
    OAuth outages at slightly lower extraction quality.
    """
    from config import STATE_DIR

    chroma_mode = os.environ.get("CHROMA_MODE", "server")
    chroma_host = os.environ.get("CHROMA_HOST", "127.0.0.1")
    chroma_port = int(os.environ.get("CHROMA_PORT", "8765"))
    # 2026-05-16 incident: rebuilt data lives in `kanae_memories_safe`
    # (infer=False verbatim). Once `kanae_memories_infer` rebuild finishes,
    # swap by setting MEM0_COLLECTION_NAME=kanae_memories_infer in .env
    # and restarting the bot. No code change required.
    collection_name = os.environ.get("MEM0_COLLECTION_NAME", "kanae_memories_safe")

    if chroma_mode == "server":
        vector_cfg = {
            "collection_name": collection_name,
            "host": chroma_host,
            "port": chroma_port,
        }
    else:
        db_path = os.path.join(STATE_DIR, "mem0_db")
        os.makedirs(db_path, exist_ok=True)
        vector_cfg = {
            "collection_name": collection_name,
            "path": db_path,
        }

    # LLM provider comes from the shared fallback chain (P1-e) so this config
    # can never disagree with get_anthropic_client()'s direct-call path.
    desired = _desired_llm_provider()
    llm_model = os.environ.get("MEM0_LLM_MODEL", "claude-haiku-4-5-20251001")
    llm_config = {
        "model": llm_model,
        "temperature": 0.1,
        "max_tokens": 2000,
    }

    if desired == "openrouter":
        log.warning(
            "Anthropic OAuth unavailable — falling back to %s via OpenRouter",
            _DEEPSEEK_V4_FLASH,
        )
        llm_provider = "openrouter"
        llm_config = {
            "model": _DEEPSEEK_V4_FLASH,
            "temperature": 0.1,
            "max_tokens": 2000,
            # Mem0's openrouter provider wraps the OpenAI-compatible API.
            "api_key": _OPENROUTER_API_KEY,
            "openai_base_url": "https://openrouter.ai/api/v1",
        }
    elif desired == "opencode":
        log.warning(
            "Anthropic OAuth unavailable — falling back to %s via OpenCode Zen",
            _DEEPSEEK_V4_FLASH,
        )
        # OpenCode Zen speaks OpenAI-compatible API at api.opencode.ai
        llm_provider = "openai"
        llm_config = {
            "model": _DEEPSEEK_V4_FLASH,
            "temperature": 0.1,
            "max_tokens": 2000,
            # mem0 1.0.11 OpenAIConfig uses `api_key` (not `openai_api_key`);
            # `openai_base_url` is still accepted.
            "api_key": _OPENCODE_API_KEY,
            "openai_base_url": "https://api.opencode.ai/v1",
        }
    elif desired is None:
        log.error(
            "Anthropic OAuth unavailable and no fallback key configured "
            "(OPENROUTER_API_KEY / OPENCODE_API_KEY) — Mem0 LLM calls will fail"
        )
        llm_provider = os.environ.get("MEM0_LLM_PROVIDER", "anthropic")
    else:
        # "anthropic" or an explicit MEM0_LLM_PROVIDER override.
        llm_provider = desired

    config: dict = {
        "llm": {
            "provider": llm_provider,
            "config": llm_config,
        },
        "vector_store": {
            "provider": "chroma",
            "config": vector_cfg,
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


def _ping_chromadb_if_server_mode() -> None:
    """When CHROMA_MODE=server, verify the ChromaDB HTTP server is reachable.

    Raises an exception on failure so the caller can mark init as failed
    and trigger the cooldown-based retry path.
    """
    if os.environ.get("CHROMA_MODE", "server") != "server":
        return
    host = os.environ.get("CHROMA_HOST", "127.0.0.1")
    port = int(os.environ.get("CHROMA_PORT", "8765"))
    import requests

    r = requests.get(f"http://{host}:{port}/api/v2/heartbeat", timeout=5)
    r.raise_for_status()


def _get_mem0():
    """Lazy-init singleton Mem0 instance with cooldown-based retry.

    On init failure, sets _init_failed_at and returns None for the next
    _INIT_RETRY_COOLDOWN_SEC seconds before retrying. This allows
    automatic recovery after ChromaDB systemd-restarts.

    Provider drift (P1-e): the instance remembers which LLM provider it was
    built with; when the desired provider changes (OAuth failover kicked in,
    or OAuth came back after a DeepSeek fallback) the singleton is rebuilt on
    the next access instead of silently keeping a stale provider forever —
    the split-brain the plan flagged as a live operational risk.
    """
    global _mem0_instance, _mem0_provider, _init_failed_at

    if _mem0_instance is not None:
        desired = _desired_llm_provider()
        if desired == _mem0_provider:
            return _mem0_instance
        log.warning(
            "Mem0 LLM provider drift (%s -> %s) — rebuilding instance",
            _mem0_provider,
            desired,
        )
        _mem0_instance = None
    if (
        _init_failed_at is not None
        and (time.monotonic() - _init_failed_at) < _INIT_RETRY_COOLDOWN_SEC
    ):
        return None

    # Initialize OAuth BEFORE creating any Anthropic clients
    if not oauth_init():
        log.error("OAuth init failed — Mem0 LLM calls will not work")

    try:
        _ping_chromadb_if_server_mode()
        from mem0 import Memory

        cfg = _build_config()
        _mem0_instance = Memory.from_config(cfg)
        _mem0_provider = _desired_llm_provider()
        _init_failed_at = None
        vs_cfg = cfg["vector_store"]["config"]
        store_id = vs_cfg.get("path") or f"{vs_cfg.get('host')}:{vs_cfg.get('port')}"
        endpoint = (
            f"file://{os.path.realpath(vs_cfg['path'])}"
            if vs_cfg.get("path")
            else f"http://{vs_cfg.get('host')}:{vs_cfg.get('port')}"
        )
        _mem0_instance.storage_identity = {
            "backend": str(cfg["vector_store"]["provider"]),
            "endpoint": endpoint,
            "collection": str(vs_cfg.get("collection_name") or ""),
        }
        log.info(
            "Mem0 ready — llm=%s provider=%s embedder=%s store=chroma(%s)",
            cfg["llm"]["config"]["model"],
            cfg["llm"]["provider"],
            cfg["embedder"]["config"]["model"],
            store_id,
        )
        integrity_mode = os.environ.get("MEM0_INTEGRITY_MODE", "legacy").lower()
        if integrity_mode in {"shadow", "canonical"}:
            from services.mem0.integrity import (
                ensure_projection_consumer,
                get_application_service,
            )

            service = get_application_service()
            if service.mode == "canonical":
                service.verify_canonical_startup(_mem0_instance)
            ensure_projection_consumer(lambda: _mem0_instance)
    except Exception as exc:
        log.error("Mem0 init failed: %s", exc, exc_info=True)
        _mem0_instance = None
        _init_failed_at = time.monotonic()

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
    # 初回 init は ChromaDB heartbeat (同期 requests) + Memory.from_config を
    # 含むためイベントループから直接呼ばない（最大5秒のループ停止になる）。
    mem = await asyncio.to_thread(_get_mem0)
    if not mem:
        return []
    try:
        await ensure_valid_token()  # refresh OAuth if needed
        _refresh_mem0_client_token()
        kwargs: dict = {"user_id": user_id}
        if limit is not None:
            kwargs["limit"] = limit
        raw = await asyncio.to_thread(mem.get_all, **kwargs)
        return (
            raw.get("results", [])
            if isinstance(raw, dict)
            else (raw if isinstance(raw, list) else [])
        )
    except Exception as exc:
        log.warning("Mem0 get_all error: %s", exc)
        return []


async def delete_memory(memory_id: str) -> bool:
    """Delete a memory through the lifecycle application service."""
    # 初回 init は ChromaDB heartbeat (同期 requests) + Memory.from_config を
    # 含むためイベントループから直接呼ばない（最大5秒のループ停止になる）。
    mem = await asyncio.to_thread(_get_mem0)
    if not mem:
        return False
    try:
        await ensure_valid_token()  # refresh OAuth if needed
        _refresh_mem0_client_token()
        from services.mem0.integrity import get_application_service

        service = get_application_service()
        return await asyncio.to_thread(service.delete, mem, memory_id)
    except Exception as exc:
        log.warning("Mem0 delete error: %s", exc)
        return False

"""Mem0 singleton + OAuth token refresh + low-level housekeeping (get_all/delete)."""

import asyncio
import logging
import os
import time

from services.oauth_manager import (
    OAuthRefreshFailed,
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
    from services.codex_app_server import auxiliary
    if auxiliary.enabled():
        return auxiliary.CodexTextClient()
    oauth_init()  # idempotent
    await _refresh_token_if_available()
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


async def _refresh_token_if_available() -> None:
    """A revoked Claude session must not disable independent memory backends.

    Only the expected OAuth failure is handled. Cancellation and unexpected
    programming errors still propagate. OAuth clears hard-expired tokens
    before raising, so provider selection cannot reuse the revoked token.
    """
    from services.codex_app_server import auxiliary
    if auxiliary.enabled():
        return
    try:
        await ensure_valid_token()
    except OAuthRefreshFailed:
        log.warning("Mem0 OAuth unavailable; retaining non-Claude memory paths")


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
    from services.codex_app_server import auxiliary
    if auxiliary.enabled():
        return 'codex'
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

    if desired == 'codex':
        from config import CODEX_APP_SERVER_MODEL
        from services.mem0.codex_llm import configuration
        config['llm'] = configuration(CODEX_APP_SERVER_MODEL)

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
    if _desired_llm_provider() != 'codex' and not oauth_init():
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
# Batch get — one Chroma round trip for a set of ids (explore-C #4)
# ---------------------------------------------------------------------------
# ``mem.get(id)`` is one Chroma ``collection.get(ids=[id])`` HTTP round trip.
# The graph sibling-fetch hot path fans those out N-wide (``asyncio.gather`` +
# Semaphore), so a recall turn issues up to ~300 of them. ChromaDB's
# ``collection.get`` natively accepts a *list* of ids, so the fan-out collapses
# into a single round trip.
#
# Output invariance (Lean ``KanaeVerify.ChromaBatchGet``): for a distinct id
# list the batch returns the *same set*, in request order when projected by the
# caller, drops missing ids identically, and never adds a key outside the
# request. The per-record dict is byte-identical to ``mem.get(id)``.

_BATCH_PROMOTED_PAYLOAD_KEYS = ("user_id", "agent_id", "run_id", "actor_id", "role")
_BATCH_CORE_AND_PROMOTED_KEYS = {
    "data", "hash", "created_at", "updated_at", "id", *_BATCH_PROMOTED_PAYLOAD_KEYS,
}


def _format_vector_record(record_id: str, payload: dict | None) -> dict:
    """Shape one Chroma record into the exact dict ``Memory.get(id)`` returns.

    Mirrors ``mem0.memory.main.Memory.get`` line-for-line: ``MemoryItem``
    model dump (keeps the ``score``/``metadata`` keys), promoted payload keys,
    then the remaining payload fields collected under ``metadata``.
    """
    from mem0.configs.base import MemoryItem
    from mem0.memory.main import _normalize_iso_timestamp_to_utc

    payload = payload or {}
    item = MemoryItem(
        id=record_id,
        memory=payload.get("data", ""),
        hash=payload.get("hash"),
        created_at=_normalize_iso_timestamp_to_utc(payload.get("created_at")),
        updated_at=_normalize_iso_timestamp_to_utc(payload.get("updated_at")),
    ).model_dump()
    for key in _BATCH_PROMOTED_PAYLOAD_KEYS:
        if key in payload:
            item[key] = payload[key]
    extra = {k: v for k, v in payload.items() if k not in _BATCH_CORE_AND_PROMOTED_KEYS}
    if extra:
        item["metadata"] = extra
    return item


def _per_id_get_map(mem, ids) -> dict:
    """Fallback: the original N+1, returned as an id→fact map (misses dropped)."""
    out: dict = {}
    for i in ids:
        try:
            fact = mem.get(i)
        except Exception:
            fact = None
        if fact:
            out[i] = fact
    return out


def batch_get_facts_sync(mem, ids) -> dict:
    """One-round-trip batch equivalent of ``{i: mem.get(i)}`` for present ids.

    Returns a mapping ``{id: fact}`` containing only ids that exist; callers
    project it back in their own request order (so ordering is their loop's
    responsibility, exactly as with the per-id version). The request is
    de-duplicated and non-str/empty ids are dropped before the round trip.

    Falls back to per-id ``mem.get`` when the backing store is not a
    Chroma-style ``collection`` or the batch call raises — never returning
    fewer facts than the per-id loop would. Sync; wrap in ``to_thread``.
    """
    seen: set = set()
    uniq: list = []
    for i in ids:
        if isinstance(i, str) and i and i not in seen:
            seen.add(i)
            uniq.append(i)
    if not uniq:
        return {}

    vs = getattr(mem, "vector_store", None)
    coll = getattr(vs, "collection", None)
    coll_get = getattr(coll, "get", None)
    if coll is None or not callable(coll_get):
        return _per_id_get_map(mem, uniq)

    try:
        raw = coll_get(ids=uniq)  # single Chroma round trip
    except Exception as exc:
        log.warning("batch get failed (%s); falling back to per-id", type(exc).__name__)
        return _per_id_get_map(mem, uniq)

    # A real Chroma get returns {"ids": [...], "metadatas": [...], ...} with list
    # values. Anything else (None, malformed, a test double) → fall back to the
    # per-id path so we are never *less* correct than the original loop.
    if not isinstance(raw, dict):
        return _per_id_get_map(mem, uniq)
    got_ids = raw.get("ids")
    metadatas = raw.get("metadatas")
    if not isinstance(got_ids, list):
        return _per_id_get_map(mem, uniq)
    if not isinstance(metadatas, list):
        metadatas = []
    # chromadb's query path nests one level ([[...]]); the get path is flat.
    # Normalise both, same as mem0's ChromaDB._parse_output.
    if got_ids and isinstance(got_ids[0], list):
        got_ids = got_ids[0]
    if metadatas and isinstance(metadatas[0], list):
        metadatas = metadatas[0]

    out: dict = {}
    for idx, rid in enumerate(got_ids):
        payload = metadatas[idx] if idx < len(metadatas) else None
        out[rid] = _format_vector_record(rid, payload)
    return out


# ---------------------------------------------------------------------------
# Low-level housekeeping (direct Mem0 access — used by weekly_mem0_compact.py)
# ---------------------------------------------------------------------------


async def get_all_memories(user_id: str, limit: int | None = None, *, strict: bool = False) -> list[dict]:
    """Retrieve memories for a user.

    `limit` is forwarded to Mem0 / ChromaDB so the underlying query can stop
    early instead of fetching the entire collection and slicing in Python.
    Pass None (default) to fetch all (used by the weekly housekeeping cron;
    avoid on hot paths).
    ``strict=True`` distinguishes backend unavailability from an empty namespace
    for explicit correction and deletion receipts. The alpha facade has the same API.
    """
    # 初回 init は ChromaDB heartbeat (同期 requests) + Memory.from_config を
    # 含むためイベントループから直接呼ばない（最大5秒のループ停止になる）。
    mem = await asyncio.to_thread(_get_mem0)
    if not mem:
        if strict:
            raise RuntimeError("Mem0 is unavailable")
        return []
    try:
        await _refresh_token_if_available()
        _refresh_mem0_client_token()
        kwargs: dict = {"user_id": user_id}
        if limit is not None:
            kwargs["limit"] = limit
        raw = await asyncio.to_thread(mem.get_all, **kwargs)
        if strict and not (isinstance(raw, list) or (isinstance(raw, dict) and isinstance(raw.get("results"), list))):
            raise RuntimeError("Mem0 returned an invalid memory listing")
        return (
            raw.get("results", [])
            if isinstance(raw, dict)
            else (raw if isinstance(raw, list) else [])
        )
    except Exception as exc:
        log.warning("Mem0 get_all error: %s", exc)
        if strict:
            raise
        return []


async def delete_memory(memory_id: str) -> bool:
    """Delete a memory through the lifecycle application service."""
    # 初回 init は ChromaDB heartbeat (同期 requests) + Memory.from_config を
    # 含むためイベントループから直接呼ばない（最大5秒のループ停止になる）。
    mem = await asyncio.to_thread(_get_mem0)
    if not mem:
        return False
    try:
        await _refresh_token_if_available()
        _refresh_mem0_client_token()
        from services.mem0.integrity import get_application_service

        service = get_application_service()
        return await asyncio.to_thread(service.delete, mem, memory_id)
    except Exception as exc:
        log.warning("Mem0 delete error: %s", exc)
        return False

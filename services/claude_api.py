"""Direct Anthropic API client — matches free-code services/api/claude.ts + client.ts pattern.

OAuth via oauth_manager (single source of truth), streaming with stall detection,
message normalization, attribution header, metadata.

Reference: free-code/src/services/api/claude.ts, client.ts, constants/betas.ts
"""

import hashlib
import json
import logging
import os
import time
import uuid

import anthropic

from config import (
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    MAX_HISTORY,
    MAX_MESSAGES_PER_CHANNEL,
    STREAM_IDLE_TIMEOUT_S,
    STREAM_STALL_THRESHOLD_S,
    SYSTEM_PROMPT,
)
from services.oauth_manager import (
    ensure_valid_token,
    get_access_token,
    init as oauth_init,
)
from services.session import (
    append_jsonl,
    fix_history_integrity,
    trim_history,
)

# ---------------------------------------------------------------------------
# Constants (free-code: constants/betas.ts, constants/system.ts)
# ---------------------------------------------------------------------------
CC_VERSION = "2.1.92"
CC_ENTRYPOINT = "sdk-py"

# Betas for Opus (free-code: getAllModelBetas)
MODEL_BETAS = [
    "claude-code-20250219",       # non-haiku
    "oauth-2025-04-20",           # OAuth subscriber
    "interleaved-thinking-2025-05-14",  # ISP
    "context-1m-2025-08-07",      # 1M context (Opus)
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",  # 1P only
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic client — delegates auth to oauth_manager
# ---------------------------------------------------------------------------
_cached_client: anthropic.Anthropic | None = None
_cached_token: str | None = None


_session_id = str(uuid.uuid4())


def _get_attribution_header(messages_str: str) -> str:
    """Build attribution header (free-code: constants/system.ts getAttributionHeader)."""
    fingerprint = hashlib.sha256(
        (messages_str + CC_VERSION).encode()
    ).hexdigest()[:16]
    return f"x-anthropic-billing-header: cc_version={CC_VERSION}.{fingerprint}; cc_entrypoint={CC_ENTRYPOINT};"


def _get_metadata() -> dict:
    """Build metadata object (free-code: getAPIMetadata)."""
    return {
        "user_id": json.dumps({
            "session_id": _session_id,
        }),
    }


def get_client() -> anthropic.Anthropic:
    """Get or create Anthropic client. Token managed by oauth_manager."""
    global _cached_client, _cached_token

    oauth_init()  # ensure SDK is patched (idempotent)

    current_token = get_access_token()
    if _cached_client is not None and current_token == _cached_token:
        return _cached_client

    _cached_client = anthropic.Anthropic(
        default_headers={
            "x-app": "cli",
            "User-Agent": f"claude-cli/{CC_VERSION} (external, {CC_ENTRYPOINT})",
            "X-Claude-Code-Session-Id": _session_id,
        },
        timeout=600.0,
        max_retries=10,
    )
    _cached_token = current_token
    log.info("Anthropic client created (via oauth_manager)")
    return _cached_client


# ---------------------------------------------------------------------------
# DGM-specific client (no SDK retries — DGM handles its own retry logic)
# ---------------------------------------------------------------------------
_cached_dgm_client: anthropic.Anthropic | None = None
_cached_dgm_token: str | None = None


def get_dgm_client() -> anthropic.Anthropic:
    """Get Anthropic client for DGM with max_retries=0 to avoid retry amplification."""
    global _cached_dgm_client, _cached_dgm_token

    oauth_init()

    current_token = get_access_token()
    if _cached_dgm_client is not None and current_token == _cached_dgm_token:
        return _cached_dgm_client

    _cached_dgm_client = anthropic.Anthropic(
        default_headers={
            "x-app": "cli",
            "User-Agent": "claude-code/1.0",
        },
        timeout=600.0,
        max_retries=0,
    )
    _cached_dgm_token = current_token
    log.info("DGM Anthropic client created (max_retries=0, via oauth_manager)")
    return _cached_dgm_client


# ---------------------------------------------------------------------------
# Message normalization (free-code: normalizeMessagesForAPI)
# ---------------------------------------------------------------------------
def normalize_messages(history: list[dict]) -> list[dict]:
    """Merge consecutive user messages, hoist tool_results to front.

    Strips extra fields (timestamp, channel_id, etc.) — API only accepts role+content.
    """
    if not history:
        return []
    normalized: list[dict] = []
    for raw_msg in history:
        msg = {"role": raw_msg["role"], "content": raw_msg["content"]}
        if normalized and normalized[-1]["role"] == msg["role"] == "user":
            prev_content = normalized[-1]["content"]
            curr_content = msg["content"]
            if isinstance(prev_content, str) and isinstance(curr_content, str):
                normalized[-1]["content"] = prev_content + "\n\n" + curr_content
            elif isinstance(prev_content, list) and isinstance(curr_content, list):
                tool_results = []
                other_blocks = []
                for b in prev_content:
                    (tool_results if isinstance(b, dict) and b.get("type") == "tool_result" else other_blocks).append(b)
                for b in curr_content:
                    (tool_results if isinstance(b, dict) and b.get("type") == "tool_result" else other_blocks).append(b)
                normalized[-1]["content"] = tool_results + other_blocks
            else:
                normalized.append(msg)
        else:
            normalized.append(msg)
    return normalized


# ---------------------------------------------------------------------------
# Streaming (free-code: queryModelWithStreaming)
# ---------------------------------------------------------------------------
MAX_RETRIES = 6
# Phase F2: was 15.0 — worst-case backoff sum was 15+30+60+120+240+480 = 945 s,
# during which the user saw no activity (no status_callback wired here) and
# Discord's typing indicator long expired. 5.0 gives a worst-case sum of
# ~315 s (5+10+20+40+80+160) which still rides through transient 429 bursts
# but keeps the UX bearable. Guarded against a malformed env var so a typo
# in systemd EnvironmentFile does not crash the bot at import time.
def _retry_backoff_base() -> float:
    raw = os.environ.get("CLAUDE_API_RETRY_BACKOFF_BASE", "5.0")
    try:
        return float(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Invalid CLAUDE_API_RETRY_BACKOFF_BASE=%r, falling back to 5.0", raw,
        )
        return 5.0


RETRY_BACKOFF_BASE = _retry_backoff_base()


def _build_cached_system(system_prompt: str) -> list[dict]:
    """Wrap system prompt with cache_control for Anthropic prompt caching.

    The server caches the KV state for this prefix (5-min TTL). Consecutive
    requests with the same system prompt skip re-encoding those tokens,
    cutting input cost by ~90% on the static portion.
    """
    return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]


def _inject_cache_breakpoints(messages: list[dict]) -> list[dict]:
    """Add a cache breakpoint to the second-to-last user message.

    This lets the API cache all tokens up to that point, so the next turn
    only processes the latest user message + assistant reply as new tokens.
    We target the second-to-last user turn (not the last) because the last
    one is the new input that changes every request.
    """
    if len(messages) < 4:
        return messages
    # Find the second-to-last user message
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) < 2:
        return messages
    target_idx = user_indices[-2]
    # Deep-copy only the target message to avoid mutating the original history
    messages = list(messages)
    msg = dict(messages[target_idx])
    content = msg["content"]
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}},
        ]
    elif isinstance(content, list):
        # Clone the last block and add cache_control
        content = list(content)
        last_block = dict(content[-1])
        last_block["cache_control"] = {"type": "ephemeral"}
        content[-1] = last_block
        msg["content"] = content
    messages[target_idx] = msg
    return messages


def _stream_once(api_client: anthropic.Anthropic, messages: list[dict], system_prompt: str, response_chunks: list[str]) -> None:
    """Single streaming attempt. No retry. Called from to_thread."""
    client_request_id = str(uuid.uuid4())
    messages_str = json.dumps(messages, ensure_ascii=False)[:2000]
    # H-8: attribution must NOT be part of the system prompt — the model
    # should never read billing metadata as instructions. Send it as an
    # opaque extra_header that only the API billing pipeline interprets.
    attribution_value = _get_attribution_header(messages_str)
    # Strip the "x-anthropic-billing-header: " prefix to get just the value,
    # since the header name is conveyed by the dict key.
    if ":" in attribution_value:
        attribution_value = attribution_value.split(":", 1)[1].strip().rstrip(";")

    cached_system = _build_cached_system(system_prompt)
    cached_messages = _inject_cache_breakpoints(messages)

    with api_client.beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=cached_system,
        messages=cached_messages,
        betas=MODEL_BETAS,
        metadata=_get_metadata(),
        extra_headers={
            "x-client-request-id": client_request_id,
            "x-anthropic-billing-header": attribution_value,
        },
    ) as stream:
        last_event_time = time.monotonic()
        for text in stream.text_stream:
            now = time.monotonic()
            gap = now - last_event_time
            if gap > STREAM_STALL_THRESHOLD_S:
                log.warning("Stream stall: %.1fs gap", gap)
            if gap > STREAM_IDLE_TIMEOUT_S:
                log.error("Stream idle timeout: %.1fs", gap)
                break
            last_event_time = now
            response_chunks.append(text)
        # Log cache performance from the final message usage
        final_msg = stream.get_final_message()
        if final_msg and final_msg.usage:
            usage = final_msg.usage
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            log.info(
                "Prompt cache: input=%d cache_read=%d cache_create=%d hit_rate=%.0f%%",
                input_tokens, cache_read, cache_create,
                (cache_read / (input_tokens + cache_read + cache_create) * 100)
                if (input_tokens + cache_read + cache_create) > 0 else 0,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def call_direct(
    prompt: str,
    channel_id: int,
    conversation_history: dict[int, list[dict]],
    conv_context: str = "",
) -> str:
    """Call Claude API directly with streaming. No CLI, no tools."""
    import asyncio

    is_new = channel_id not in conversation_history
    if is_new:
        conversation_history[channel_id] = []
        # LRU eviction
        if hasattr(conversation_history, "move_to_end"):
            conversation_history.move_to_end(channel_id)
        while len(conversation_history) > MAX_HISTORY:
            if hasattr(conversation_history, "popitem"):
                conversation_history.popitem(last=False)
            else:
                break

    history = conversation_history[channel_id]
    fix_history_integrity(history)

    # Seed new channels with thread context (free-code: initialMessages)
    if is_new and conv_context:
        history.append({"role": "user", "content": f"[過去のスレッド履歴]\n{conv_context}"})
        history.append({"role": "assistant", "content": "了解しました。スレッドの文脈を把握しました。"})

    user_msg = {"role": "user", "content": prompt}
    history.append(user_msg)
    trim_history(history, MAX_MESSAGES_PER_CHANNEL)
    await asyncio.to_thread(append_jsonl, channel_id, user_msg)

    try:
        await ensure_valid_token()  # refresh OAuth if needed
        api_client = get_client()
        messages = normalize_messages(history)
        response_chunks: list[str] = []

        for attempt in range(MAX_RETRIES):
            try:
                response_chunks = []
                await asyncio.to_thread(_stream_once, api_client, messages, SYSTEM_PROMPT, response_chunks)
                break  # success
            except anthropic.RateLimitError:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                    log.warning("Rate limited (attempt %d/%d), retrying in %.0fs", attempt + 1, MAX_RETRIES, wait)
                    await asyncio.sleep(wait)
                else:
                    raise

        full_response = "".join(response_chunks)
        assistant_msg = {"role": "assistant", "content": full_response}
        history.append(assistant_msg)
        await asyncio.to_thread(append_jsonl, channel_id, assistant_msg)
        return full_response.strip() or "(No response from Claude)"

    except Exception as e:
        log.error("Direct API error: %s", e, exc_info=True)
        if history and history[-1]["role"] == "user":
            history.pop()
        return "❌ APIエラーが発生しました。少し待って再試行してください。"

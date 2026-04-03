"""Direct Anthropic API client — matches free-code services/api/claude.ts + client.ts pattern.

OAuth token caching, streaming with stall detection, message normalization.
"""

import json
import logging
import time
import uuid

import anthropic

from config import (
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    CREDENTIALS_PATH,
    MAX_HISTORY,
    MAX_MESSAGES_PER_CHANNEL,
    STREAM_IDLE_TIMEOUT_S,
    STREAM_STALL_THRESHOLD_S,
    SYSTEM_PROMPT,
    TOKEN_REFRESH_BUFFER_MS,
)
from services.session import (
    append_jsonl,
    fix_history_integrity,
    trim_history,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OAuth client (free-code: getAnthropicClient + checkAndRefreshOAuthTokenIfNeeded)
# ---------------------------------------------------------------------------
_cached_client: anthropic.Anthropic | None = None
_cached_token_expires_at: int = 0


def _load_oauth_tokens() -> dict:
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        return json.load(f)["claudeAiOauth"]


def _is_token_expired(expires_at: int | None) -> bool:
    if expires_at is None:
        return True
    now_ms = int(time.time() * 1000)
    return now_ms + TOKEN_REFRESH_BUFFER_MS >= expires_at


def get_client() -> anthropic.Anthropic:
    """Get or create Anthropic client, refreshing token from disk if expired."""
    global _cached_client, _cached_token_expires_at

    if _cached_client is not None and not _is_token_expired(_cached_token_expires_at):
        return _cached_client

    tokens = _load_oauth_tokens()
    _cached_token_expires_at = tokens.get("expiresAt", 0)
    session_id = str(uuid.uuid4())
    _cached_client = anthropic.Anthropic(
        api_key=None,
        auth_token=tokens["accessToken"],
        default_headers={
            "x-app": "cli",
            "User-Agent": "claude-code/1.0",
            "X-Claude-Code-Session-Id": session_id,
            "anthropic-beta": "oauth-2025-04-20",
        },
        timeout=600.0,
        max_retries=2,
    )
    log.info("Anthropic client initialized (token expires at %d)", _cached_token_expires_at)
    return _cached_client


# ---------------------------------------------------------------------------
# Message normalization (free-code: normalizeMessagesForAPI)
# ---------------------------------------------------------------------------
def normalize_messages(history: list[dict]) -> list[dict]:
    """Merge consecutive user messages, hoist tool_results to front."""
    if not history:
        return []
    normalized: list[dict] = []
    for msg in history:
        if normalized and normalized[-1]["role"] == msg["role"] == "user":
            prev_content = normalized[-1]["content"]
            curr_content = msg["content"]
            if isinstance(prev_content, str) and isinstance(curr_content, str):
                normalized[-1]["content"] = prev_content + "\n\n" + curr_content
            elif isinstance(prev_content, list) and isinstance(curr_content, list):
                tool_results = [b for b in prev_content + curr_content
                                if isinstance(b, dict) and b.get("type") == "tool_result"]
                other_blocks = [b for b in prev_content + curr_content
                                if not (isinstance(b, dict) and b.get("type") == "tool_result")]
                normalized[-1]["content"] = tool_results + other_blocks
            else:
                normalized.append(msg)
        else:
            normalized.append(msg)
    return normalized


# ---------------------------------------------------------------------------
# Streaming (free-code: queryModelWithStreaming)
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 10.0  # seconds (10s → 20s → 40s)


def _stream_sync(api_client: anthropic.Anthropic, history: list[dict], response_chunks: list[str]) -> None:
    """Synchronous streaming with stall/idle detection and retry (free-code pattern)."""
    messages = normalize_messages(history)
    client_request_id = str(uuid.uuid4())

    for attempt in range(MAX_RETRIES):
        try:
            with api_client.beta.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
                betas=[
                    "claude-code-20250219",
                    "oauth-2025-04-20",
                    "interleaved-thinking-2025-05-14",
                    "context-management-2025-06-27",
                    "prompt-caching-scope-2026-01-05",
                ],
                extra_headers={"x-client-request-id": client_request_id},
            ) as stream:
                last_event_time = time.monotonic()
                for text in stream.text_stream:
                    now = time.monotonic()
                    gap = now - last_event_time
                    if gap > STREAM_STALL_THRESHOLD_S:
                        log.warning("Stream stall: %.1fs gap between events", gap)
                    if gap > STREAM_IDLE_TIMEOUT_S:
                        log.error("Stream idle timeout: %.1fs, aborting", gap)
                        break
                    last_event_time = now
                    response_chunks.append(text)
            return  # success
        except anthropic.RateLimitError:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                log.warning("Rate limited (attempt %d/%d), retrying in %.0fs", attempt + 1, MAX_RETRIES, wait)
                time.sleep(wait)
                response_chunks.clear()
            else:
                raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def call_direct(
    prompt: str,
    channel_id: int,
    conversation_history: dict[int, list[dict]],
    thread_context: str = "",
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
    if is_new and thread_context:
        history.append({"role": "user", "content": f"[過去のスレッド履歴]\n{thread_context}"})
        history.append({"role": "assistant", "content": "了解しました。スレッドの文脈を把握しました。"})

    user_msg = {"role": "user", "content": prompt}
    history.append(user_msg)
    trim_history(history, MAX_MESSAGES_PER_CHANNEL)
    append_jsonl(channel_id, user_msg)

    try:
        api_client = get_client()
        response_chunks: list[str] = []

        await asyncio.to_thread(_stream_sync, api_client, history, response_chunks)

        full_response = "".join(response_chunks)
        assistant_msg = {"role": "assistant", "content": full_response}
        history.append(assistant_msg)
        append_jsonl(channel_id, assistant_msg)
        return full_response.strip() or "(No response from Claude)"

    except Exception as e:
        log.error("Direct API error: %s", e, exc_info=True)
        if history and history[-1]["role"] == "user":
            history.pop()
        return f"❌ API error: {e}"

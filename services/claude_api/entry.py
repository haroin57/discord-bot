"""Top-level entry — assembles every claude_api submodule into call_with_tools.

The signature matches services/claude_sdk.py:call_with_tools verbatim so
bot.py and web_api.py don't have to change. Internally, we:

1. acquire the right concurrency semaphore (admin vs restricted),
2. parse + sanitize ``<attached_images>`` blocks if has_image_attachment,
3. resolve / drop the session (same MCP-subset rule as the SDK path),
4. spawn the MCP server set under an AsyncExitStack we own,
5. build an AsyncAnthropic client (OAuth Bearer or API-key bypass),
6. build the tools list + can_use_tool gate + execute_tool dispatcher,
7. delegate to run_agent_loop under a StallWatchdog,
8. wrap AgentLoopResult into SessionInfo and return.

Retry handles transient errors with the same per-channel budget + exponential
backoff as the SDK path. Permanent errors (auth / context overflow / max_turns)
return the localized error message without retrying.
"""

import asyncio
import logging
import time
from contextlib import AsyncExitStack

import anthropic

from config import (
    ADMIN_MAX_TURNS,
    CLAUDE_MAX_PARALLEL,
    CWD,
    RESTRICTED_CLAUDE_MAX_PARALLEL,
    RESTRICTED_SYSTEM_PROMPT,
    MCP_SERVERS,
    SYSTEM_PROMPT,
)
from services.mcp_selector import (
    get_mcp_servers_by_keys,
    pick_mcp_servers,
)
from services.session import SessionInfo, append_jsonl, load_history_channel

from services.claude_api.client import (
    CC_VERSION,
    MODEL_BETAS,
)
from services.claude_api.errors import (
    _error_message,
    _is_transient,
    classify_anthropic_error,
)
from services.claude_api.mcp_client import (
    McpPool,
    mcp_tools_to_anthropic,
)
from services.claude_api.restricted import (
    RESTRICTED_MAX_TURNS,
    make_can_use_tool,
)
from services.claude_api.retry import (
    DEFAULT_MAX_ATTEMPTS,
    TRANSIENT_MAX_ATTEMPTS,
    check_and_record_retry_budget,
    compute_backoff,
)
from services.claude_api.session_store import (
    _clear_session_time,
    _is_session_fresh,
    _record_session_time,
)
from services.claude_api.stall import StallWatchdog
from services.claude_api.status import _safe_status
from services.claude_api.tool_loop import AgentLoopResult, run_agent_loop
from services.claude_api.tools import (
    ToolContext,
    builtin_tools_for_api,
    execute_builtin_tool,
)

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"

# Concurrency: same defaults as services/claude_sdk.py.
claude_semaphore = asyncio.Semaphore(CLAUDE_MAX_PARALLEL)
restricted_claude_semaphore = asyncio.Semaphore(RESTRICTED_CLAUDE_MAX_PARALLEL)


# Image processing moved to services/claude_api/image_processor.py.
# Re-export with the original underscore-prefixed names so existing
# call sites and test patches keep working.
from services.claude_api.image_processor import (
    IMAGE_MAX_BYTES as _IMAGE_MAX_BYTES,
    IMAGE_MAX_COUNT as _IMAGE_MAX_COUNT,
    extract_attached_image_blocks as _extract_attached_image_blocks,
    safe_image_path as _safe_image_path,
)


def _resolve_session(
    sessions: dict[int, SessionInfo],
    channel_id: int,
    chosen_mcp_keys: set[str],
) -> tuple[dict[str, dict], bool]:
    """Apply the MCP-subset / TTL rules and return ``(mcp_cfg, resumable)``.

    Mirrors services/claude_sdk.py:606-625. May mutate ``sessions`` by
    invalidating an incompatible-MCP session.

    ``resumable`` is True when the caller may load prior turns from the
    JSONL transcript and seed them into the agent loop. False whenever
    the session is missing, stale, or MCP-incompatible.
    """
    info = sessions.get(channel_id)
    if info is None or not _is_session_fresh(channel_id):
        if info is not None:
            sessions.pop(channel_id, None)
            _clear_session_time(channel_id)
        return get_mcp_servers_by_keys(list(chosen_mcp_keys)), False

    stored = set(info.mcp_server_keys or [])
    if chosen_mcp_keys <= stored:
        # Compatible session — reuse the same MCP set we had before so
        # tool_use blocks from earlier turns still route correctly.
        return get_mcp_servers_by_keys(list(stored)), True

    # MCP mismatch — drop the session and start fresh.
    sessions.pop(channel_id, None)
    _clear_session_time(channel_id)
    return get_mcp_servers_by_keys(list(chosen_mcp_keys)), False


# Anthropic client construction moved to services/claude_api/client_factory.py.
# Re-export with the legacy name for any direct callers / test patches.
from services.claude_api.client_factory import make_api_client as _make_api_client


async def call_with_tools(
    prompt: str,
    channel_id: int,
    sessions: dict[int, SessionInfo],
    status_callback=None,
    restricted: bool = False,
    raw_user_text: str = "",
    is_thread: bool = False,
    has_image_attachment: bool = False,
) -> tuple[str, SessionInfo | None]:
    """Direct-API replacement for services/claude_sdk.py:call_with_tools.

    Signature is fixed (Plan locked condition). ``is_thread`` is currently
    unused in the API path — the SDK path used it to gate resume eligibility,
    but the API path's resume is history-based so the gate is irrelevant.
    """
    sema = restricted_claude_semaphore if restricted else claude_semaphore
    async with sema:
        # 1. Parse images out of the prompt (only when caller signaled them
        #    + when admin / restricted-with-image-handling actually wants
        #    them inlined). Done before MCP spawn so a bad path fails fast.
        prompt_text = prompt
        image_blocks: list[dict] = []
        if has_image_attachment:
            prompt_text, image_blocks = _extract_attached_image_blocks(prompt)

        # 2. MCP selection (admin only).
        if restricted:
            chosen_mcp_cfg: dict[str, dict] = {}
        else:
            chosen_mcp_cfg = pick_mcp_servers(raw_user_text)
        chosen_mcp_keys = set(chosen_mcp_cfg.keys())

        # 3. Session resume (admin only — restricted is stateless).
        resumable = False
        if not restricted:
            chosen_mcp_cfg, resumable = _resolve_session(
                sessions, channel_id, chosen_mcp_keys
            )
            chosen_mcp_keys = set(chosen_mcp_cfg.keys())

        # 4. Build the user message with optional inlined images.
        if image_blocks:
            user_content: str | list[dict] = (
                image_blocks + [{"type": "text", "text": prompt_text}]
            )
        else:
            user_content = prompt_text

        # 4b. Load prior conversation turns from JSONL when the session is
        # resumable. Anthropic Messages API is stateless: server-side resume
        # by session_id does not exist, so we must replay the transcript on
        # every turn — the same trick the official `claude --resume` does
        # against ~/.claude/projects/<cwd>/<session_id>.jsonl.
        prior_history: list[dict] = []
        if resumable:
            try:
                prior_history = await asyncio.to_thread(
                    load_history_channel, channel_id
                )
            except Exception as exc:
                log.warning("[ch=%d] history load failed: %s", channel_id, exc)
                prior_history = []

        # 5. Per-call settings
        if restricted:
            system_prompt = RESTRICTED_SYSTEM_PROMPT
            cwd = ATTACHMENT_DIR
            max_turns = RESTRICTED_MAX_TURNS
            advertised_tools: list[dict] = []  # restricted advertises none
        else:
            system_prompt = SYSTEM_PROMPT
            cwd = CWD
            max_turns = ADMIN_MAX_TURNS
            advertised_tools = builtin_tools_for_api(restricted=False)

        can_use_tool = make_can_use_tool(restricted=restricted)
        tool_ctx = ToolContext(cwd=cwd, restricted=restricted, channel_id=channel_id)

        async with AsyncExitStack() as stack:
            # 6. MCP pool: keyword-matched servers are spawned eagerly, the
            # rest are spawned lazily the moment kanae calls one of their
            # tools. restricted users get an empty config so auto-spawn
            # can't surface tools they shouldn't see.
            pool_available = {} if restricted else MCP_SERVERS
            pool = McpPool(stack=stack, available_configs=pool_available)
            if chosen_mcp_cfg:
                await pool.spawn_eager(list(chosen_mcp_cfg))
            advertised_tools = advertised_tools + mcp_tools_to_anthropic(pool.handles)

            async def _execute_tool(name: str, args: dict) -> str:
                if name.startswith("mcp__"):
                    return await pool.call_tool(name, args)
                return await execute_builtin_tool(name, args, tool_ctx)

            # 7. API client
            api_client = await _make_api_client(restricted)

            # 8. Retry loop around run_agent_loop
            max_attempts = DEFAULT_MAX_ATTEMPTS
            response_text: str | None = None
            result: AgentLoopResult | None = None
            for attempt in range(TRANSIENT_MAX_ATTEMPTS):
                if attempt > 0 and not check_and_record_retry_budget(channel_id):
                    log.warning("Retry budget exhausted for ch=%d", channel_id)
                    return _error_message("unknown"), None

                async with StallWatchdog(channel_id, status_callback) as watchdog:
                    try:
                        # Seed each attempt with a fresh copy of the prior
                        # transcript so a partial / failed retry does not
                        # poison the next attempt. run_agent_loop appends
                        # the current user turn itself, so we do NOT add
                        # the new prompt here.
                        history_for_loop: list[dict] = list(prior_history)
                        result = await run_agent_loop(
                            prompt if not image_blocks else user_content,
                            channel_id,
                            history=history_for_loop,
                            system_prompt=system_prompt,
                            tools_for_api=advertised_tools,
                            can_use_tool=can_use_tool,
                            execute_tool=_execute_tool,
                            status_callback=status_callback,
                            max_turns=max_turns,
                            stall_watchdog=watchdog,
                            api_client=api_client,
                            model=MODEL,
                            extra_betas=MODEL_BETAS,
                            active_mcp_keys=sorted(chosen_mcp_keys),
                        )
                        response_text = result.text
                        break
                    except asyncio.CancelledError:
                        # Stall watchdog fired or shutdown — surface as timeout.
                        log.warning("[ch=%d] cancelled during agent loop", channel_id)
                        return f"⏰ タイムアウトしました", None
                    except anthropic.AnthropicError as exc:
                        category = classify_anthropic_error(exc)
                        log.error(
                            "[ch=%d] api error attempt %d/%d type=%s: %s",
                            channel_id, attempt + 1, TRANSIENT_MAX_ATTEMPTS,
                            category, exc,
                        )
                        if category in ("auth", "context_overflow", "max_turns"):
                            sessions.pop(channel_id, None)
                            _clear_session_time(channel_id)
                            return _error_message(category), None
                        if _is_transient(category) and max_attempts < TRANSIENT_MAX_ATTEMPTS:
                            max_attempts = TRANSIENT_MAX_ATTEMPTS
                        if attempt >= max_attempts - 1:
                            sessions.pop(channel_id, None)
                            _clear_session_time(channel_id)
                            return _error_message(category), None
                        backoff = compute_backoff(attempt)
                        await _safe_status(
                            status_callback,
                            f"🔄 リトライ中 ({attempt + 1}/{max_attempts})",
                        )
                        await asyncio.sleep(backoff)

        # 9. Wrap into SessionInfo
        if response_text is None or result is None:
            return _error_message("unknown"), None
        session_info = SessionInfo(
            session_id=result.synthetic_session_id or "",
            mcp_server_keys=result.active_mcp_keys,
        )
        if result.synthetic_session_id:
            sessions[channel_id] = session_info
            _record_session_time(channel_id)

        # 10. Persist this turn's user/assistant pair to the JSONL transcript
        # so the next call (within TTL) can replay it.
        #
        # We persist the *full* prompt bot.py built for this turn — including
        # the <retrieved_memories> block, the trusted/strict header, and the
        # <untrusted_user_input> wrapper — not just raw_user_text. This is the
        # only way Mem0 context survives into the next turn: bot.py skips Mem0
        # search for short follow-ups (MEM0_MIN_QUERY_LENGTH=20) and the API
        # is stateless, so without this persistence a short reply after a
        # long Mem0-seeded turn would lose all recall.
        #
        # We use prompt_text (the image-blocks-stripped text) so massive
        # base64 image payloads do not bloat the JSONL.
        # Failures here (disk full, perms) must not abort the request.
        if prompt_text and response_text:
            try:
                await asyncio.to_thread(
                    append_jsonl,
                    channel_id,
                    {"role": "user", "content": prompt_text},
                )
                await asyncio.to_thread(
                    append_jsonl,
                    channel_id,
                    {"role": "assistant", "content": response_text},
                )
            except Exception as exc:
                log.warning("[ch=%d] history append failed: %s", channel_id, exc)

        return response_text, session_info if result.synthetic_session_id else None

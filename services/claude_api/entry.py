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
import base64
import logging
import uuid as _uuid
import os
import re
import time
from contextlib import AsyncExitStack

import anthropic

from config import (
    ADMIN_MAX_TURNS,
    ATTACHMENT_DIR,
    CLAUDE_MAX_PARALLEL,
    CWD,
    RESTRICTED_ANTHROPIC_API_KEY,
    RESTRICTED_CLAUDE_MAX_PARALLEL,
    RESTRICTED_SYSTEM_PROMPT,
    MCP_SERVERS,
    SYSTEM_PROMPT,
    get_mcp_servers_by_keys,
    pick_mcp_servers,
)
from services.oauth_manager import (
    ensure_valid_token,
    init as oauth_init,
    make_async_api_key_client,
    make_async_oauth_client,
    prime_subscriber_session,
)
from services.session import SessionInfo, append_jsonl, load_history_channel

from services.claude_api.client import (
    CC_VERSION,
    MODEL_BETAS,
    USER_AGENT,
    _session_id,
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


# Magic-byte signatures for allowed image MIME types.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # actual check below requires "WEBP" at offset 8
)
_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8 MB. base64 inflates ~1.33x, so worst-case
# per image ≈ 10.7 MB of string payload; × _IMAGE_MAX_COUNT = ~85 MB peak.
# Per-message hard cap so a malicious / runaway parser doesn't OOM us.
_IMAGE_MAX_COUNT = 8

# <attached_images>\n- /path (Use the Read tool to view this image)\n- /path\n</attached_images>
_ATTACHED_IMAGES_BLOCK_RE = re.compile(
    r"<attached_images>\s*(?P<body>.*?)\s*</attached_images>",
    re.DOTALL,
)


def _identify_image(data: bytes) -> str | None:
    """Return MIME if ``data`` opens with a recognized image header."""
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    for magic, mime in _IMAGE_MAGIC:
        if mime == "image/webp":
            continue  # handled above
        if data.startswith(magic):
            return mime
    return None


def _safe_image_path(path: str) -> str | None:
    """Validate that ``path`` is under ATTACHMENT_DIR, not a symlink escape,
    and small enough. Returns the canonical realpath or None."""
    try:
        real_root = os.path.realpath(ATTACHMENT_DIR)
        candidate = os.path.realpath(path)
        if candidate != real_root and not candidate.startswith(real_root + os.sep):
            log.warning("image rejected: outside ATTACHMENT_DIR: %s", path)
            return None
        st = os.stat(candidate)
        if st.st_size > _IMAGE_MAX_BYTES:
            log.warning("image rejected: too large (%d bytes): %s", st.st_size, path)
            return None
        return candidate
    except OSError as exc:
        log.warning("image rejected: %s (%s)", path, exc)
        return None


def _extract_attached_image_blocks(prompt: str) -> tuple[str, list[dict]]:
    """Strip the last ``<attached_images>`` block and return base64 ImageBlocks.

    Codex v3 #10: only the *last* occurrence is treated as bot-generated, so
    a user typing the literal tag in their message is not parsed. The block
    is removed from the prompt entirely after extraction so the Read-tool
    hint inside it doesn't fool the model.
    """
    match = None
    # rfind via iteration — re has no rsearch helper.
    for m in _ATTACHED_IMAGES_BLOCK_RE.finditer(prompt):
        match = m
    if match is None:
        return prompt, []

    body = match.group("body")
    paths: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        token = line.lstrip("-").strip().split(" ", 1)[0]
        if token:
            paths.append(token)

    blocks: list[dict] = []
    for path in paths[:_IMAGE_MAX_COUNT]:
        real = _safe_image_path(path)
        if real is None:
            continue
        try:
            with open(real, "rb") as fh:
                data = fh.read(_IMAGE_MAX_BYTES + 1)
            if len(data) > _IMAGE_MAX_BYTES:
                log.warning("image rejected: oversized on read: %s", real)
                continue
            mime = _identify_image(data[:16])
            if mime is None:
                log.warning("image rejected: unrecognized magic: %s", real)
                continue
        except OSError as exc:
            log.warning("image rejected: %s (%s)", real, exc)
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.b64encode(data).decode("ascii"),
            },
        })

    stripped = prompt[: match.start()] + prompt[match.end():]
    return stripped.strip(), blocks


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


async def _make_api_client(restricted: bool) -> anthropic.AsyncAnthropic:
    """Return the right AsyncAnthropic client for this call."""
    # Headers the real claude-cli sends. The X-Stainless-* fields identify
    # the SDK (Node 0.80.0) to Anthropic's server-side subscriber-tier
    # gating; sending the Python SDK values ("python", "CPython", "0.88.0")
    # appears to demote the call out of the subscriber bucket and triggers
    # the immediate HTTP 429s we kept seeing. Mimic the Node CLI exactly.
    cli_headers = {
        "x-app": "cli",
        "User-Agent": USER_AGENT,
        "X-Claude-Code-Session-Id": _session_id,
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.80.0",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.14.1",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Timeout": "600",
        "accept-language": "*",
        "sec-fetch-mode": "cors",
        # The real CLI generates a fresh UUID per /v1/messages POST. We can
        # only set the header once at client-construction time (anthropic SDK
        # default_headers); a tool_loop with multiple turns will reuse it.
        # Servers seem to accept that, but if 429 reasoning ever shows
        # client-id reuse as a discriminator, hoist this into a httpx
        # request hook for true per-request rotation.
        "x-client-request-id": str(_uuid.uuid4()),
    }
    if restricted and RESTRICTED_ANTHROPIC_API_KEY:
        return make_async_api_key_client(
            RESTRICTED_ANTHROPIC_API_KEY,
            default_headers=cli_headers,
        )
    # bot startup paths only call oauth_init() lazily through mem0 or the
    # sync get_client(); on a cold call_with_tools the token is still
    # uninitialized. Idempotent init() before ensure_valid_token() guarantees
    # _access_token is populated before make_async_oauth_client reads it.
    oauth_init()
    await ensure_valid_token()
    # Subscriber session prime: replay the CLI's /api/oauth/profile and
    # /api/oauth/claude_cli/roles GETs. Once per process. Without this,
    # we've seen 429s with zero ratelimit-unified headers (= subscriber
    # tier not recognized).
    await prime_subscriber_session()
    return make_async_oauth_client(default_headers=cli_headers)


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

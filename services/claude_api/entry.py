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
import os
import time
from contextlib import AsyncExitStack

import anthropic
import httpx

from config import (
    ADMIN_MAX_TURNS,
    ATTACHMENT_DIR,
    CLAUDE_MAX_PARALLEL,
    CWD,
    FABLE_REFUSAL_FALLBACK_ENABLED,
    FABLE_REFUSAL_FALLBACK_MODEL,
    HISTORY_COMPACT_TRIGGER_CHARS,
    HISTORY_COMPACT_TRIGGER_TOKENS,
    RESTRICTED_CLAUDE_MAX_PARALLEL,
    RESTRICTED_SYSTEM_PROMPT,
    RETRY_AFTER_MAX_S,
    MCP_SERVERS,
    SYSTEM_PROMPT,
)
from services.claude_api.compaction import (
    compact_channel_history,
    estimate_replay_chars,
    record_proactive_compaction,
    reset_rapid_refill,
)
from services.mcp_selector import (
    get_mcp_servers_by_keys,
    pick_mcp_servers,
)
from services.session import (
    SessionInfo,
    append_jsonl,
    build_compact_history_entries,
    load_api_replay_channel,
    load_history_channel,
    prompt_has_context_snapshot,
)

from services.claude_api.client import (
    CC_VERSION,
    MODEL_BETAS,
)
from services.claude_api.errors import (
    _error_message,
    _is_transient,
    classify_anthropic_error,
    is_non_persistable_reply,
)
from services.claude_api.mcp_client import (
    McpPool,
    mcp_tools_to_anthropic,
    static_mcp_tools_to_anthropic,
)
from services.claude_api.restricted import (
    RESTRICTED_MAX_TURNS,
    make_can_use_tool,
)
from services.claude_api.retry import (
    TRANSIENT_MAX_ATTEMPTS,
    check_and_record_retry_budget,
    compute_backoff,
    retry_after_from_headers,
)
from services.claude_api.ratelimit import append_rate_limit_details
from services.claude_api.session_store import (
    _clear_session_time,
    _get_sticky_model,
    _is_session_fresh,
    _record_session_time,
    _record_sticky_model,
)
from services.claude_api.stall import StallWatchdog
from services.claude_api.status import _safe_status
from services.claude_api.tool_loop import (
    REFUSAL_FALLBACK_HEADER,
    AgentLoopResult,
    get_last_context_tokens,
    run_agent_loop,
)
from services.claude_api.tools import (
    ToolContext,
    builtin_tools_for_api,
    execute_builtin_tool,
)

log = logging.getLogger(__name__)

MODEL = "claude-fable-5"

# MCP servers whose tool schemas we advertise on every admin API request.
# The process is still spawned lazily via McpPool.call_tool() on first use.
# Mem0 sits here so Kanae can record durable facts without selector
# keywords having to detect a "remember this" intent first.
# WEB tier (x-scraper / news-curator / brave-search) joined 2026-05-16
# after a turn-30 exhaustion: pick_mcp_servers() dropped WEB on a short
# follow-up, so the tools fell out of advertised_tools and Kanae burned
# the whole agent loop hunting for tools that were no longer in her
# schema list. Static-advertise + lazy-spawn matches the mem0 pattern.
ALWAYS_ADVERTISED_MCP_KEYS = {
    "mem0", "x-scraper", "news-curator", "brave-search",
    # Market data (2026-06-13): replaces the Brave-search-the-finance-page
    # pattern for quotes, and feeds the VI strategy signal checks. Static
    # schemas + lazy spawn, same as the rest.
    "nikkei-indexes", "yfj-quote",
}

_SNAPSHOT_MAX_AGE_S = int(os.environ.get("SNAPSHOT_MAX_AGE_S", "900"))


def _anthropic_error_headers(exc: BaseException):
    resp = getattr(exc, "response", None)
    return getattr(resp, "headers", None) if resp is not None else None


def _classified_error_message(category: str, exc: BaseException | None = None) -> str:
    message = _error_message(category)
    if category == "rate_limit" and exc is not None:
        return append_rate_limit_details(message, _anthropic_error_headers(exc))
    return message


def _prepend_context_snapshot(
    user_content: "str | list[dict]", snapshot: str,
) -> "str | list[dict]":
    """Prepend the cached Mem0 snapshot to the next user turn.

    Only used on short follow-ups whose freshly-built prompt does not
    already carry a ``<retrieved_memories>`` / ``<kanae_self_memory>``
    block. Materializing the snapshot here keeps recall continuity
    across the cheap-prompt/expensive-prompt boundary that
    MEM0_MIN_QUERY_LENGTH creates in bot.py.
    """
    if not snapshot:
        return user_content
    prefix = f"{snapshot}\n\n"
    if isinstance(user_content, str):
        return prefix + user_content
    blocks = list(user_content)
    for idx, block in enumerate(blocks):
        if isinstance(block, dict) and block.get("type") == "text":
            updated = dict(block)
            updated["text"] = prefix + str(updated.get("text", ""))
            blocks[idx] = updated
            return blocks
    return blocks + [{"type": "text", "text": snapshot}]


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
        return get_mcp_servers_by_keys(list(stored)), True

    # New MCP keys needed — merge into the existing set instead of
    # dropping the session. Old tool_use blocks in the JSONL transcript
    # stay valid because every stored key is preserved; new tools are
    # simply added. This avoids the will_resume/resumable mismatch that
    # previously caused complete context loss on MCP tier escalation.
    merged = stored | chosen_mcp_keys
    info.mcp_server_keys = sorted(merged)
    log.info(
        "[ch=%d] MCP merge: %s + %s → %s",
        channel_id, sorted(stored), sorted(chosen_mcp_keys - stored),
        info.mcp_server_keys,
    )
    return get_mcp_servers_by_keys(info.mcp_server_keys), True


# Anthropic client construction moved to services/claude_api/client_factory.py.
# Re-export with the legacy name for any direct callers / test patches.
from services.claude_api.client_factory import make_api_client as _make_api_client


async def call_with_tools(
    prompt: str,
    channel_id: int,
    sessions: dict[int, SessionInfo],
    status_callback=None,
    incremental_text_callback=None,
    text_stream_callback=None,
    tool_invocation_callback=None,
    restricted: bool = False,
    raw_user_text: str = "",
    is_thread: bool = False,
    has_image_attachment: bool = False,
    author: str = "",
) -> tuple[str, SessionInfo | None, bool]:
    """Direct-API replacement for services/claude_sdk.py:call_with_tools.

    ``author`` is the stable speaker label of the message sender (e.g. "先輩").
    It is stamped onto the user turn in the JSONL transcript so that, when a
    later turn resumes this channel, replayed history can tell apart messages
    from different admins sharing the channel. Empty string = unknown/unlabelled
    (no attribution recorded — preserves the pre-existing behaviour).

    Returns ``(response_text, session_info, actually_resumed)`` where
    ``actually_resumed`` is True only when JSONL history was loaded and
    fed to the Messages API. bot.py uses this to detect a mismatch with
    its own ``will_resume`` flag (which gates Lean vs Full prompt).

    ``is_thread`` is currently unused in the API path — the SDK path used
    it to gate resume eligibility, but the API path's resume is
    history-based so the gate is irrelevant.
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
            always_advertised_mcp_keys: set[str] = set()
        else:
            chosen_mcp_cfg = pick_mcp_servers(raw_user_text)
            always_advertised_mcp_keys = set(ALWAYS_ADVERTISED_MCP_KEYS)
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
        # The transcript is replayed for every non-restricted turn, session
        # boundary or not (ADR-0010 phase 3) — MCP keys merge instead of
        # invalidating (see _resolve_session), so replayed tool_use blocks
        # stay valid. The Mem0 snapshot is likewise always inherited — it is
        # opaque text, not tool_use / tool_result blocks (see bug postmortem
        # in docs/plans/pdf-pdf-pdf-pdf-tingly-moth.md).
        prior_history: list[dict] = []
        latest_context_snapshot = ""
        if not restricted:
            try:
                loaded_history, loaded_snapshot, loaded_snapshot_ts = (
                    await asyncio.to_thread(
                        load_api_replay_channel, channel_id
                    )
                )
            except Exception as exc:
                log.warning("[ch=%d] history load failed: %s", channel_id, exc)
            else:
                latest_context_snapshot = loaded_snapshot
                if latest_context_snapshot and loaded_snapshot_ts:
                    try:
                        from datetime import datetime, timezone
                        snap_dt = datetime.fromisoformat(loaded_snapshot_ts)
                        if snap_dt.tzinfo is None:
                            snap_dt = snap_dt.replace(tzinfo=timezone.utc)
                        age = (datetime.now(timezone.utc) - snap_dt).total_seconds()
                        if age > _SNAPSHOT_MAX_AGE_S:
                            log.info(
                                "[ch=%d] stale context snapshot dropped (age=%.0fs)",
                                channel_id, age,
                            )
                            latest_context_snapshot = ""
                    except (ValueError, TypeError):
                        pass
                # ADR-0010 phase 3: seed the prior transcript regardless of
                # session freshness — the same thing the official CLI's
                # --resume does. Before this, a restart / 30-min TTL lapse
                # (resumable=False) silently dropped the whole conversation;
                # now the context survives the boundary, and when the window
                # has grown past the Fable-safe budget it is compacted below
                # before the request goes out.
                prior_history = loaded_history

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
            # admin path advertises builtin tools on every request. The
            # earlier `_needs_builtin_tools` keyword gate misclassified
            # natural follow-ups ("次のページ読んで", "20〜40読んで") as
            # pure chat and broke ongoing tool workflows. The ~686-token
            # schema lands in prompt cache from the second turn on, so the
            # warm-time cost is ~68 tokens — not worth the UX hit.
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
                # Always-advertised MCPs (Mem0) are spawned lazily on first
                # tool_use; everything else is spawned eagerly so its tools
                # show up in advertised_tools below.
                eager_keys = [
                    key for key in chosen_mcp_cfg
                    if key not in always_advertised_mcp_keys
                ]
                if eager_keys:
                    await pool.spawn_eager(eager_keys)
            advertised_tools = (
                advertised_tools
                + static_mcp_tools_to_anthropic(always_advertised_mcp_keys)
                + mcp_tools_to_anthropic(pool.handles)
            )

            async def _execute_tool(name: str, args: dict) -> str:
                if name.startswith("mcp__"):
                    return await pool.call_tool(name, args)
                return await execute_builtin_tool(name, args, tool_ctx)

            # 7. API client
            api_client = await _make_api_client(restricted)

            # 7b. History compaction (ADR-0010 phase 3): when the replay
            # window has outgrown the Fable-safe budget, fold the older
            # portion into a compact-summary row (official-CLI compact
            # semantics) and reload the collapsed window. Every failure mode
            # is non-fatal — the raw history still works via the refusal
            # fallback downstream, just at higher cost.
            if not restricted and prior_history:
                try:
                    # Token-first trigger (audit #7): once a previous turn has
                    # reported real usage, gate on tokens (chars mis-estimate
                    # JP/code transcripts by 2-4x). The char gate remains as
                    # the cold-start fallback (no usage observed yet — fresh
                    # process). force=True on the token path so the summary
                    # call's own char gate cannot veto a token decision.
                    over_tokens = (
                        get_last_context_tokens(channel_id)
                        > HISTORY_COMPACT_TRIGGER_TOKENS
                    )
                    _replay_chars = estimate_replay_chars(prior_history)
                    over_chars = _replay_chars > HISTORY_COMPACT_TRIGGER_CHARS
                    if over_tokens or over_chars:
                        log.info(
                            "[ch=%d] boundary compaction trigger: "
                            "last_context_tokens=%d (trigger=%d, over=%s) "
                            "replay_chars=%d (trigger=%d, over=%s) force=%s",
                            channel_id,
                            get_last_context_tokens(channel_id),
                            HISTORY_COMPACT_TRIGGER_TOKENS, over_tokens,
                            _replay_chars,
                            HISTORY_COMPACT_TRIGGER_CHARS, over_chars,
                            over_tokens,
                        )
                    if (
                        (over_tokens or over_chars)
                        and await compact_channel_history(
                            channel_id, api_client, status_callback,
                            force=over_tokens,
                        )
                    ):
                        reloaded, _snap, _ts = await asyncio.to_thread(
                            load_api_replay_channel, channel_id
                        )
                        if reloaded:
                            log.info(
                                "[ch=%d] boundary compaction reload: "
                                "replay %d → %d turns, %d → %d chars",
                                channel_id,
                                len(prior_history), len(reloaded),
                                _replay_chars, estimate_replay_chars(reloaded),
                            )
                            prior_history = reloaded
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "[ch=%d] history compaction failed (non-fatal): %s",
                        channel_id, exc,
                    )

            # 8. Retry loop around run_agent_loop. The loop range
            # (TRANSIENT_MAX_ATTEMPTS) is the single source of truth for the
            # attempt count — there is no separate max_attempts to keep in
            # sync (ADR-0006 removed the off-by-one that masked overloaded
            # failures as "unknown").
            response_text: str | None = None
            result: AgentLoopResult | None = None
            # Fallback-ladder state (audit #6): set when a 404 (model renamed/
            # retired) or near-exhausted 529 streak forces the request onto the
            # fallback model for the remaining attempts. Distinct from the
            # refusal sticky pin — this is a transport-level escape, scoped to
            # this request only.
            forced_model: str | None = None
            # Rapid-refill accounting for the in-loop proactive compaction
            # (audit #19): True when this request triggered one.
            proactive_fired = False

            async def _proactive_compact() -> "list[dict] | None":
                """In-loop compaction callback for run_agent_loop (audit #3).

                Folds the prior transcript mid-request and returns the
                reloaded (collapsed) replay window, or None when compaction
                did not happen (disabled / breaker open / summary failed).
                """
                nonlocal proactive_fired
                ok = await compact_channel_history(
                    channel_id, api_client, status_callback, force=True,
                )
                if not ok:
                    return None
                proactive_fired = True
                record_proactive_compaction(channel_id)
                reloaded, _s, _t = await asyncio.to_thread(
                    load_api_replay_channel, channel_id
                )
                return reloaded

            for attempt in range(TRANSIENT_MAX_ATTEMPTS):
                if attempt > 0 and not check_and_record_retry_budget(channel_id):
                    log.warning("Retry budget exhausted for ch=%d", channel_id)
                    return _error_message("unknown"), None, False

                async with StallWatchdog(channel_id, status_callback) as watchdog:
                    try:
                        # Seed each attempt with a fresh copy of the prior
                        # transcript so a partial / failed retry does not
                        # poison the next attempt. run_agent_loop appends
                        # the current user turn itself, so we do NOT add
                        # the new prompt here.
                        history_for_loop: list[dict] = list(prior_history)
                        user_content_for_loop = (
                            prompt if not image_blocks else user_content
                        )
                        # Replay the latest Mem0 snapshot only when the
                        # current prompt does not already carry one (short
                        # follow-up after a MEM0_MIN_QUERY_LENGTH-gated
                        # turn). Done once per request, not once per JSONL
                        # row, so we don't accumulate stale recall.
                        if (
                            latest_context_snapshot
                            and not prompt_has_context_snapshot(prompt_text)
                        ):
                            user_content_for_loop = _prepend_context_snapshot(
                                user_content_for_loop, latest_context_snapshot
                            )
                            log.info(
                                "[PROMPT_BREAKDOWN ch=%d] context_snapshot re-injected: chars=%d resumable=%s",
                                channel_id, len(latest_context_snapshot), resumable,
                            )
                        # Measure final payload going to the Messages API
                        if isinstance(user_content_for_loop, str):
                            _user_chars = len(user_content_for_loop)
                            _user_blocks = 1
                        else:
                            _user_chars = sum(
                                len(b.get("text", "")) for b in user_content_for_loop
                                if isinstance(b, dict)
                            )
                            _user_blocks = len(user_content_for_loop)
                        _hist_chars = sum(
                            len(m.get("content", "")) if isinstance(m.get("content"), str) else 0
                            for m in history_for_loop if isinstance(m, dict)
                        )
                        log.info(
                            "[PROMPT_BREAKDOWN ch=%d] api_payload: prior_turns=%d prior_chars=%d "
                            "user_chars=%d user_blocks=%d system_chars=%d tools=%d resumable=%s attempt=%d",
                            channel_id, len(history_for_loop), _hist_chars,
                            _user_chars, _user_blocks, len(system_prompt or ""),
                            len(advertised_tools), resumable, attempt + 1,
                        )
                        # ADR-0010: once this session fell back to Opus, pin
                        # subsequent turns to Opus. Re-trying Fable on every
                        # turn at long context re-pays the full cache_creation
                        # (~240k tokens observed) only to get refused again,
                        # and the alternating models keep both prompt caches
                        # cold (cache_read=0 on every Fable turn). Restricted
                        # channels stay on the default model (no pinning).
                        sticky_model = (
                            _get_sticky_model(channel_id) if not restricted else None
                        )
                        effective_model = forced_model or sticky_model or MODEL
                        result = await run_agent_loop(
                            user_content_for_loop,
                            channel_id,
                            history=history_for_loop,
                            system_prompt=system_prompt,
                            tools_for_api=advertised_tools,
                            can_use_tool=can_use_tool,
                            execute_tool=_execute_tool,
                            status_callback=status_callback,
                            incremental_text_callback=incremental_text_callback,
                            text_stream_callback=text_stream_callback,
                            tool_invocation_callback=tool_invocation_callback,
                            max_turns=max_turns,
                            stall_watchdog=watchdog,
                            api_client=api_client,
                            model=effective_model,
                            extra_betas=MODEL_BETAS,
                            active_mcp_keys=sorted(
                                chosen_mcp_keys | always_advertised_mcp_keys
                            ),
                            # On a Fable 5 refusal (content classifier, not a
                            # real safety stop here), retry once on Opus 4.8 and
                            # surface the switch. Only when the primary model is
                            # actually Fable — never fall Opus back onto Opus.
                            refusal_fallback_model=(
                                FABLE_REFUSAL_FALLBACK_MODEL
                                if (FABLE_REFUSAL_FALLBACK_ENABLED
                                    and "fable" in (effective_model or "").lower())
                                else None
                            ),
                            # A sticky pin means a refusal fallback already
                            # happened in this session — mark every subsequent
                            # request the way the official CLI does
                            # (x-is-refusal-fallback: true, ADR-0010).
                            extra_headers=(
                                {REFUSAL_FALLBACK_HEADER: "true"}
                                if sticky_model else None
                            ),
                            # In-loop proactive compaction (audit #3): admin
                            # channels only — restricted is stateless, there
                            # is no transcript to fold.
                            proactive_compact=(
                                _proactive_compact if not restricted else None
                            ),
                            prior_turns=len(history_for_loop),
                        )
                        response_text = result.text
                        if getattr(result, "fallback_model", None):
                            log.warning(
                                "[ch=%d] response generated via refusal fallback model=%s",
                                channel_id, result.fallback_model,
                            )
                            if not restricted:
                                # Pin the session so the next turn starts on
                                # the fallback model directly (ADR-0010).
                                _record_sticky_model(
                                    channel_id, result.fallback_model
                                )
                        break
                    except asyncio.CancelledError:
                        # Stall watchdog fired or shutdown — surface as timeout.
                        log.warning("[ch=%d] cancelled during agent loop", channel_id)
                        return f"⏰ タイムアウトしました", None, False
                    except anthropic.AnthropicError as exc:
                        category = classify_anthropic_error(exc)
                        log.error(
                            "[ch=%d] api error attempt %d/%d type=%s: %s",
                            channel_id, attempt + 1, TRANSIENT_MAX_ATTEMPTS,
                            category, exc,
                        )
                        if category == "context_overflow":
                            # History too long — session must restart so
                            # the next turn loads a shorter JSONL tail.
                            sessions.pop(channel_id, None)
                            _clear_session_time(channel_id)
                            return _classified_error_message(category, exc), None, False
                        if category == "tool_use_mismatch":
                            # tool_use/tool_result pairing 400 (audit #14):
                            # the replayed transcript desynced. Recoverable —
                            # drop the session so the next turn reloads a
                            # clean, Yy8-swept replay window instead of
                            # treating the channel as permanently broken.
                            sessions.pop(channel_id, None)
                            _clear_session_time(channel_id)
                            return _classified_error_message(category, exc), None, False
                        if category == "model_not_found":
                            # Model renamed/retired (audit #6, CLI
                            # tengu_api_model_not_found_fallback). Escape to
                            # the fallback model once; a 404 on the fallback
                            # too is a real outage.
                            if (
                                forced_model is None
                                and FABLE_REFUSAL_FALLBACK_ENABLED
                                and effective_model != FABLE_REFUSAL_FALLBACK_MODEL
                            ):
                                forced_model = FABLE_REFUSAL_FALLBACK_MODEL
                                log.warning(
                                    "[ch=%d] model %s not found → fallback to %s",
                                    channel_id, effective_model, forced_model,
                                )
                                await _safe_status(
                                    status_callback,
                                    "⚠️ モデル不在のためフォールバック中",
                                )
                                continue
                            return _classified_error_message(category, exc), None, resumable
                        if category == "auth" and attempt == 0:
                            # OAuth race: a sibling process (typically the
                            # mem0-mcp MCP server) refreshed the token while
                            # this request held an in-flight access_token,
                            # which the auth server then revoked. Reload the
                            # credentials file to pick up the new token and
                            # rebuild the API client before retrying once.
                            # A second auth failure falls through to the
                            # "real auth problem" branch below.
                            from services.oauth_manager import init as _oauth_reload
                            _oauth_reload()
                            api_client = await _make_api_client(restricted)
                            log.info(
                                "[ch=%d] auth retry after token reload (attempt %d→%d)",
                                channel_id, attempt + 1, attempt + 2,
                            )
                            await _safe_status(
                                status_callback,
                                f"🔄 認証リトライ中 ({attempt + 1}/{TRANSIENT_MAX_ATTEMPTS})",
                            )
                            continue
                        if category in ("auth", "max_turns"):
                            # auth (2nd time): genuine credential problem —
                            # reloading didn't help, so the token is truly
                            # invalid. Surface to user.
                            # max_turns: tool-loop hit the per-request turn
                            # cap — session history is still valid.
                            return _classified_error_message(category, exc), None, resumable
                        if not _is_transient(category):
                            # Permanent errors (api_exit, invalid_request,
                            # unknown) — retrying won't help. Return
                            # immediately to avoid wasting API calls.
                            return _classified_error_message(category, exc), None, resumable
                        # Transient (overloaded / rate_limit): the loop runs
                        # `range(TRANSIENT_MAX_ATTEMPTS)`, so the final attempt
                        # is `TRANSIENT_MAX_ATTEMPTS - 1`. We MUST return the
                        # correctly-classified error here on that attempt —
                        # otherwise the loop falls through with result=None and
                        # the function tail returns the "unknown" fallback
                        # ("内部エラー"), masking an overloaded as a generic
                        # failure (ADR-0006).
                        if attempt >= TRANSIENT_MAX_ATTEMPTS - 1:
                            # Transient errors exhausted retries — keep the
                            # session alive so the next user message can
                            # resume without losing context.
                            return _classified_error_message(category, exc), None, resumable
                        # Overload escape hatch (audit #6, CLI
                        # tengu_api_opus_fallback): when 529s have nearly
                        # exhausted the budget and we are still on Fable,
                        # spend the remaining attempt(s) on the fallback
                        # model instead of hammering the same overloaded pool.
                        if (
                            category == "overloaded"
                            and attempt >= TRANSIENT_MAX_ATTEMPTS - 2
                            and forced_model is None
                            and FABLE_REFUSAL_FALLBACK_ENABLED
                            and "fable" in (effective_model or "").lower()
                        ):
                            forced_model = FABLE_REFUSAL_FALLBACK_MODEL
                            log.warning(
                                "[ch=%d] overload streak on %s → trying %s",
                                channel_id, effective_model, forced_model,
                            )
                        # Honor the server's retry-after when present (audit
                        # #5): the longer of server-requested wait and our
                        # computed backoff wins (CLI lqH). A wait beyond
                        # RETRY_AFTER_MAX_S is pointless for a chat reply —
                        # surface the classified error immediately instead.
                        backoff = compute_backoff(attempt)
                        retry_after = retry_after_from_headers(
                            _anthropic_error_headers(exc)
                        )
                        if retry_after is not None:
                            if retry_after > RETRY_AFTER_MAX_S:
                                log.warning(
                                    "[ch=%d] retry-after=%.0fs exceeds max %.0fs"
                                    " — giving up early",
                                    channel_id, retry_after, RETRY_AFTER_MAX_S,
                                )
                                return _classified_error_message(category, exc), None, resumable
                            backoff = max(backoff, retry_after)
                        await _safe_status(
                            status_callback,
                            f"🔄 再試行中 ({attempt + 1}/{TRANSIENT_MAX_ATTEMPTS})"
                            f"、次は約{int(round(backoff))}秒後",
                        )
                        await asyncio.sleep(backoff)
                    except httpx.RequestError as exc:
                        # SSE streaming 中の HTTP/2 接続切断 (GOAWAY /
                        # RemoteProtocolError / ReadError 等) が、MCP の
                        # anyio TaskGroup を経由せず *裸* の httpx.RequestError
                        # として上がってくるケース。anthropic SDK はこれを
                        # AnthropicError でラップしないため above の except に
                        # 入らず、下の BaseExceptionGroup 経路 (= TaskGroup 越し
                        # にラップされた版) にも入らない。リトライせず素通り
                        # させると bot.py の generic except が「❌ エラー」を返し、
                        # 1 回再接続すれば通る transient 障害を恒久エラー扱いして
                        # しまっていた (ADR-0007)。ラップ版と同じ transient
                        # retry ロジックに合流させる。
                        log.warning(
                            "[ch=%d] transport error (bare) attempt %d/%d: %r",
                            channel_id, attempt + 1, TRANSIENT_MAX_ATTEMPTS, exc,
                        )
                        if attempt >= TRANSIENT_MAX_ATTEMPTS - 1:
                            return _error_message("overloaded"), None, resumable
                        backoff = compute_backoff(attempt)
                        await _safe_status(
                            status_callback,
                            f"🔄 接続再試行中 ({attempt + 1}/{TRANSIENT_MAX_ATTEMPTS})"
                            f"、次は約{int(round(backoff))}秒後",
                        )
                        await asyncio.sleep(backoff)
                    except BaseExceptionGroup as eg:
                        # SSE streaming中に httpx 層で接続が切れると
                        # (e.g. HTTP/2 GOAWAY mid-stream), anthropic SDK は
                        # それをラップせず、anyio TaskGroup (MCP / stdio) の
                        # 境界で ExceptionGroup として伝播する。中身が
                        # httpx.RequestError なら transient として retry。
                        # それ以外の sub-exception は上位の generic catch
                        # に re-raise する。
                        transport_eg, rest = eg.split(httpx.RequestError)
                        if transport_eg is None:
                            raise
                        exc = transport_eg.exceptions[0]
                        log.warning(
                            "[ch=%d] transport error attempt %d/%d: %r",
                            channel_id, attempt + 1, TRANSIENT_MAX_ATTEMPTS, exc,
                        )
                        if rest is not None:
                            # Mixed group — surface the non-transport sub-tree
                            # too so we don't silently swallow real bugs.
                            raise rest
                        if attempt >= TRANSIENT_MAX_ATTEMPTS - 1:
                            return _error_message("overloaded"), None, resumable
                        backoff = compute_backoff(attempt)
                        await _safe_status(
                            status_callback,
                            f"🔄 接続再試行中 ({attempt + 1}/{TRANSIENT_MAX_ATTEMPTS})"
                            f"、次は約{int(round(backoff))}秒後",
                        )
                        await asyncio.sleep(backoff)

        # 9. Wrap into SessionInfo
        if response_text is None or result is None:
            return _error_message("unknown"), None, False
        # Rapid-refill breaker bookkeeping (audit #19): a request that finished
        # without needing an in-loop compaction resets the consecutive counter.
        if not restricted and not proactive_fired:
            reset_rapid_refill(channel_id)
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
        # Compact storage (2026-05-15 token-reduction plan): instead of
        # re-saving the full rendered prompt, we keep ``raw_user_text`` plus
        # the assistant reply, capped at COMPACT_HISTORY_USER_CHARS /
        # COMPACT_HISTORY_ASSISTANT_CHARS. Mem0 / kanae_self_memory blocks
        # are pulled out into ``context_snapshot`` so a short follow-up turn
        # can still see recall context without paying for it on every row.
        # ``build_compact_history_entries`` carries the previous snapshot
        # forward when the current turn lacks a fresh one.
        # Failures here (disk full, perms) must not abort the request.
        #
        # Never persist sentinel/error replies ("(no response)", タイムアウト,
        # 混雑 etc). Replaying them into a resumed transcript primes the model
        # (fable-5 especially) to keep emitting empty/refused turns — a
        # self-reinforcing poison loop (ADR-0008). "(no response)" is truthy so
        # the old `if response_text:` guard let it through; gate on content too.
        if response_text and not is_non_persistable_reply(response_text):
            try:
                # Lossless CLI-parity replay: run_agent_loop appended THIS
                # request's full structured expansion (user prompt, assistant
                # thinking(+signature)/tool_use turns, tool_result turns, final
                # assistant) into history_for_loop in place. Everything past the
                # prior transcript is the new turns; new_turns[0] is the user
                # prompt (persisted separately as raw_user_text so the Mem0
                # snapshot is not re-baked into the transcript), and new_turns[1:]
                # is the assistant/tool_result expansion we store verbatim so a
                # later resume replays signed thinking + tool context instead of
                # flattened text (the structureless replay caused the refusals).
                # The prior-window boundary may have moved if an in-loop
                # proactive compaction replaced history[:prior_len] (audit #3);
                # result.prior_len is authoritative when set.
                _prior_len = (
                    result.prior_len
                    if result.prior_len is not None
                    else len(prior_history)
                )
                new_turns = history_for_loop[_prior_len:]
                api_messages = new_turns[1:] if len(new_turns) > 1 else None
                user_entry, assistant_entry = build_compact_history_entries(
                    raw_user_text=raw_user_text or prompt_text,
                    prompt_text=prompt_text,
                    assistant_text=response_text,
                    previous_context_snapshot=latest_context_snapshot,
                    author=author,
                    api_messages=api_messages,
                )
                await asyncio.to_thread(append_jsonl, channel_id, user_entry)
                await asyncio.to_thread(append_jsonl, channel_id, assistant_entry)
            except Exception as exc:
                log.warning("[ch=%d] history append failed: %s", channel_id, exc)

        return response_text, session_info if result.synthetic_session_id else None, resumable

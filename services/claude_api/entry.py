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
    SUBAGENT_ENABLED,
    ATTACHMENT_DIR,
    CLAUDE_MAX_PARALLEL,
    CLAUDE_MODEL,
    CWD,
    FABLE_REFUSAL_FALLBACK_ENABLED,
    FABLE_REFUSAL_FALLBACK_MODEL,
    HISTORY_COMPACT_TRIGGER_TOKENS,
    MICROCOMPACT_ENABLED,
    MICROCOMPACT_GAP_MIN,
    MICROCOMPACT_KEEP_RECENT,
    RESTRICTED_CLAUDE_MAX_PARALLEL,
    RESTRICTED_SYSTEM_PROMPT,
    RETRY_AFTER_MAX_S,
    DELIB_EVIDENCE_ENABLED,
    DELIB_JUDGE_LITE_ENABLED,
    DELIB_PLANNER_ENABLED,
    DELIB_VERIFIER_ENABLED,
    DELIB_VERIFIER_MODEL,
    DELIB_VERIFY_MAX_REVISIONS,
    EVAL_MCP_ENABLED,
    MAGI_LITE_ENABLED,
    MCP_SERVERS,
    SYSTEM_PROMPT,
    TOOL_SEARCH_ENABLED,
)
from services.claude_api.compaction import (
    RECOVERY_DIRECTIVE,
    compact_channel_history,
    compact_threshold_tokens,
    estimate_replay_chars,
    pop_pending_recovery,
    record_proactive_compaction,
    reset_rapid_refill,
    start_background_compaction,
)
from services.claude_api.microcompact import microcompact_tool_results
from services.mcp_selector import (
    get_mcp_servers_by_keys,
    select_mcp_servers,
)
from services.session import (
    SessionInfo,
    append_jsonl,
    build_compact_history_entries,
    clear_session,
    get_session,
    load_api_replay_channel,
    load_history_channel,
    prompt_has_context_snapshot,
    set_session,
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
    is_tool_search_request_error,
)
from services.claude_api.mcp_client import McpPool
from services.claude_api.tool_catalog import (
    build_advertised_tools,
    deferred_catalog_keys,
    disable_tool_search,
    is_tool_search_active,
)
from services.claude_api.evidence import new_index as new_evidence_index
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
from services.claude_api.ratelimit import (
    append_rate_limit_details,
    get_state as ratelimit_get_state,
)
from services.oauth_manager import (
    OAuthRefreshFailed,
    failback_to_primary as oauth_failback_to_primary,
    failover_to_secondary as oauth_failover_to_secondary,
    get_active_account as oauth_get_active_account,
    maybe_restore_primary as oauth_maybe_restore_primary,
)
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
    execute_builtin_tool,
)

log = logging.getLogger(__name__)

MODEL = CLAUDE_MODEL

# MCP servers whose tool schemas we advertise on every admin API request.
# The process is still spawned lazily via McpPool.call_tool() on first use.
# Mem0 sits here so Kanae can record durable facts without selector
# keywords having to detect a "remember this" intent first.
# WEB tier (x-scraper / brave-search) joined 2026-05-16 after a turn-30
# exhaustion: pick_mcp_servers() dropped WEB on a short follow-up, so the
# tools fell out of advertised_tools and Kanae burned the whole agent loop
# hunting for tools that were no longer in her schema list. Static-advertise
# + lazy-spawn matches the mem0 pattern. (news-curator removed 2026-06-19.)
ALWAYS_ADVERTISED_MCP_KEYS = {
    "mem0", "x-scraper", "brave-search",
    # Market data (2026-06-13): replaces the Brave-search-the-finance-page
    # pattern for quotes, and feeds the VI strategy signal checks. Static
    # schemas + lazy spawn, same as the rest.
    "nikkei-indexes", "yfj-quote", "polymarket",
    # みんかぶFX オーダーブック (注文/建玉の価格帯別分布)。2026-06-24。
    "minkabu-fx",
}

_SNAPSHOT_MAX_AGE_S = int(os.environ.get("SNAPSHOT_MAX_AGE_S", "900"))


# ADR-0040: checkpoint auto-resume policy — reason ごとの自動継続上限。
# tool_loop はループ検知で止まったのに自動継続したらループが再開するだけ
# なので恒久 0。予算系（wallclock/output_tokens）はコスト暴走ガードの延長に
# あたるため別上限。module-level なのは unit test のため（closure だと不可視）。
def auto_resume_cap(reason: str, *, default_max: int, budget_max: int) -> int:
    caps = {
        "turn_ceiling": default_max,
        "stream_stall": default_max,
        "wallclock": budget_max,
        "output_tokens": budget_max,
    }
    return min(caps.get(reason or "", 0), default_max)


def strip_checkpoint_note(text: str) -> str:
    """checkpoint 応答から resume ノート（手動/自動どちらも）を剥がす。

    自動継続で複数セグメントを結合するとき、中間セグメントに残った
    「続けて」/「自動で実行します」ノートは最終応答のノイズになるため。
    本文なし checkpoint（ノートのみの応答）は空文字列に落ちて、結合時に
    セグメントごと除去される。
    """
    from services.claude_api.tool_loop import _AUTO_RESUME_NOTE, _CHECKPOINT_NOTE
    out = text or ""
    for note in (
        _CHECKPOINT_NOTE,
        _AUTO_RESUME_NOTE,
        "⏸️ 作業をここまで保存しました。「続けて」で同じ文脈から再開できます。",
        "⏳ 作業をここまで保存しました。続きを自動で実行します…",
    ):
        out = out.replace("\n\n" + note, "").replace(note, "")
    return out.strip()


def _quota_exhausted_signal() -> bool:
    """subscriber 枠が構造的に尽きているか（OAuth failover の即時発動条件）。

    5h/7d バケットの rejected / utilization>=1.0 は「reset まで数時間、
    秒単位 backoff では絶対に回復しない」ことを意味する。確率的 throttle
    由来の一時的 429 はここに該当せず、従来 backoff に任せる。
    """
    st = ratelimit_get_state()
    return (
        st.five_h_status == "rejected"
        or st.seven_d_status == "rejected"
        or st.five_h_utilization >= 1.0
        or st.seven_d_utilization >= 1.0
    )


def _failover_retry_at(exc: BaseException) -> float:
    """quota 起因 failover の primary 自動復帰時刻を推定する。

    retry-after ヘッダと 5h バケットの reset 時刻の遅い方。どちらも
    取れなければ 30 分後（保守的既定）。
    """
    now = time.time()
    known: list[float] = []
    try:
        ra = retry_after_from_headers(_anthropic_error_headers(exc))
        if ra:
            known.append(now + min(float(ra), 6 * 3600.0))
    except Exception:  # noqa: BLE001
        pass
    try:
        st = ratelimit_get_state()
        if st.five_h_reset > now:
            known.append(float(st.five_h_reset))
    except Exception:  # noqa: BLE001
        pass
    return max(known) if known else now + 1800.0


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
    user_id: "int | None" = None,
) -> tuple[dict[str, dict], bool]:
    """Apply the MCP-subset / TTL rules and return ``(mcp_cfg, resumable)``.

    Mirrors services/claude_sdk.py:606-625. Reads/writes the (channel, user)
    session via the composite-key store (機能1) rather than the legacy
    channel-only ``sessions`` dict, so co-located admins don't clobber each
    other's session. ``sessions`` is kept in the signature for back-compat
    (the bootstrap dict bot.py loads) but is no longer the source of truth.

    ``resumable`` is True when the caller may load prior turns from the
    JSONL transcript and seed them into the agent loop. False whenever
    the session is missing, stale, or MCP-incompatible.
    """
    info = get_session(channel_id, user_id)
    if info is None or not _is_session_fresh(channel_id, user_id):
        if info is not None:
            clear_session(channel_id, user_id)
            _clear_session_time(channel_id, user_id)
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


async def _transient_backoff(
    attempt: int,
    backoff: float,
    *,
    status_callback,
    label: str = "再試行中",
) -> None:
    """User-visible countdown + sleep for one transient-retry arm (P3-a).

    All three except arms of the attempt loop (AnthropicError / bare
    httpx.RequestError / TaskGroup-wrapped ExceptionGroup) ended in this
    exact status+sleep dance; the backoff *value* stays caller-computed
    because the Anthropic arm merges a server retry-after into it.
    """
    await _safe_status(
        status_callback,
        f"🔄 {label} ({attempt + 1}/{TRANSIENT_MAX_ATTEMPTS})"
        f"、次は約{int(round(backoff))}秒後",
    )
    await asyncio.sleep(backoff)


async def call_with_tools(
    prompt: str,
    channel_id: int,
    sessions: dict[int, SessionInfo],
    status_callback=None,
    incremental_text_callback=None,
    text_stream_callback=None,
    tool_invocation_callback=None,
    scratch_stream_callback=None,
    scratch_finalize_callback=None,
    thinking_stream_callback=None,     # async (partial_thinking:str)->None  (計画 preview)
    thinking_finalize_callback=None,   # async ()->None  (thinking block closed)
    verify_stream_callback=None,       # async (partial_thinking:str)->None  (検証 preview)
    verify_finalize_callback=None,     # async ()->None  (verifier thinking block closed)
    progress: "dict | None" = None,
    restricted: bool = False,
    raw_user_text: str = "",
    is_thread: bool = False,
    has_image_attachment: bool = False,
    author: str = "",
    user_id: "int | None" = None,
    forced_model_override: "str | None" = None,
    forced_effort_override: "str | None" = None,
    tier: "str | None" = None,        # 熟考P2: "chat" | "deliberate" | None(=従来)
    route_judgment: bool = False,      # 熟考P5: 判断系の問い（MAGI-lite トリガー）
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
    if restricted:
        tier = None  # 二重ゲート: restricted に熟考パイプラインは適用しない
        route_judgment = False
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
            chosen_mcp_cfg = await select_mcp_servers(raw_user_text, user_id=user_id)
            always_advertised_mcp_keys = set(ALWAYS_ADVERTISED_MCP_KEYS)
            if EVAL_MCP_ENABLED and "evalsearch" in MCP_SERVERS:
                # 評価ハーネス: 凍結コーパス検索を eager spawn 経路で広告する
                # （静的 schema が無いので live handle 経由でしか tools に載らない）。
                chosen_mcp_cfg = dict(chosen_mcp_cfg)
                chosen_mcp_cfg["evalsearch"] = MCP_SERVERS["evalsearch"]
        chosen_mcp_keys = set(chosen_mcp_cfg.keys())

        # 3. Session resume (admin only — restricted is stateless).
        resumable = False
        if not restricted:
            chosen_mcp_cfg, resumable = _resolve_session(
                sessions, channel_id, chosen_mcp_keys, user_id
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

                # Time-based microcompact (ported from CLI $F_/wF_): on an idle
                # gap, clear OLD tool_result bodies (cheap, no LLM) before the
                # costly full compaction below. Gap is measured from the freshest
                # replayed snapshot timestamp; older tool outputs become a short
                # placeholder while the last KEEP_RECENT are kept verbatim.
                if MICROCOMPACT_ENABLED and prior_history and loaded_snapshot_ts:
                    try:
                        from datetime import datetime, timezone
                        _sdt = datetime.fromisoformat(loaded_snapshot_ts)
                        if _sdt.tzinfo is None:
                            _sdt = _sdt.replace(tzinfo=timezone.utc)
                        _gap_min = (
                            datetime.now(timezone.utc) - _sdt
                        ).total_seconds() / 60.0
                        prior_history, _mc_saved = microcompact_tool_results(
                            prior_history,
                            gap_minutes=_gap_min,
                            gap_threshold_min=MICROCOMPACT_GAP_MIN,
                            keep_recent=MICROCOMPACT_KEEP_RECENT,
                        )
                        if _mc_saved > 0:
                            log.info(
                                "[ch=%d] microcompact: gap=%.0fmin, cleared old "
                                "tool_results (~%d tokens reclaimed)",
                                channel_id, _gap_min, _mc_saved,
                            )
                    except (ValueError, TypeError):
                        pass

        # 5. Per-call settings
        if restricted:
            system_prompt = RESTRICTED_SYSTEM_PROMPT
            cwd = ATTACHMENT_DIR
            max_turns = RESTRICTED_MAX_TURNS
        else:
            system_prompt = SYSTEM_PROMPT
            cwd = CWD
            max_turns = ADMIN_MAX_TURNS

        from services.claude_api.permissions import wrap_permission_gate

        # free-code permission port: ルール評価（deny/ask/allow）＋Discord
        # リアクション承認（ask_bridge 経由）を既存 gate に重ねる。
        can_use_tool = wrap_permission_gate(
            make_can_use_tool(restricted=restricted), restricted=restricted,
        )
        tool_ctx = ToolContext(cwd=cwd, restricted=restricted, channel_id=channel_id)

        # 熟考 P1: tool 呼び出し台帳（観測層）。restricted には作らない。
        evidence = (
            new_evidence_index(channel_id)
            if (DELIB_EVIDENCE_ENABLED and not restricted) else None
        )

        async with AsyncExitStack() as stack:
            # 6. MCP pool: keyword-matched servers are spawned eagerly, the
            # rest are spawned lazily the moment kanae calls one of their
            # tools. restricted users get an empty config so auto-spawn
            # can't surface tools they shouldn't see.
            # Lazy spawn の対象は「広告カタログに載り得るサーバー」に限定する
            # （defer 広告の導入で発見可能性が上がるため、permission-gated
            # サーバーをツール名の幻覚/発見経由で spawn できる既存ギャップも
            # ここで閉じる）。
            hot_mcp_keys = set(chosen_mcp_keys) | set(always_advertised_mcp_keys)
            if restricted:
                pool_available: dict[str, dict] = {}
            else:
                _spawnable = deferred_catalog_keys(user_id) | hot_mcp_keys
                pool_available = {
                    k: v for k, v in MCP_SERVERS.items() if k in _spawnable
                }
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

            # Agent tool（メインパイプライン subagent。CLI 純正と足並み）。admin のみ。
            _agent_schema: dict | None = None
            if SUBAGENT_ENABLED and not restricted:
                from services.claude_api.agent_tool import agent_tool_schema
                _agent_schema = agent_tool_schema()

            def _build_tools() -> list[dict]:
                """広告 tools 配列を（再）構築する。

                Tool Search の 400 auto-off 後にリトライ内で従来構成へ
                組み直せるよう closure 化してある。フラグ OFF 時は従来実装と
                byte-identical（tool_catalog.build_advertised_tools 参照）。
                """
                return build_advertised_tools(
                    restricted=restricted,
                    live_handles=pool.handles,
                    always_keys=set(always_advertised_mcp_keys),
                    hot_keys=hot_mcp_keys,
                    user_id=user_id,
                    tool_search_enabled=(
                        TOOL_SEARCH_ENABLED and is_tool_search_active()
                    ),
                    agent_tool_schema=_agent_schema,
                )

            advertised_tools = _build_tools()

            # Agent tool（メインパイプライン subagent）の実行は api_client と
            # effective_model に依存するが、どちらも _execute_tool が定義された後で
            # 確定する。closure からは可変 dict 経由で参照する。
            # parent_watchdog は subagent 実行中に親 StallWatchdog にハートビートを
            # 送るための参照。これが無いと subagent 実行中の沈黙で親が idle_timeout
            # で cancel され「subagent 完了前に親が exit」が起きる（haroin 2026-06-25）。
            _agent_dispatch: dict = {"api_client": None, "model": None, "watchdog": None}

            async def _execute_tool(name: str, args: dict) -> str:
                if name == "Agent" and SUBAGENT_ENABLED and not restricted:
                    if _agent_dispatch["api_client"] is None:
                        return "<tool_use_error>Agent: dispatcher not initialised</tool_use_error>"
                    from services.claude_api.agent_tool import (
                        AgentToolCtx,
                        execute_agent_tool,
                    )
                    return await execute_agent_tool(
                        args,
                        AgentToolCtx(
                            api_client=_agent_dispatch["api_client"],
                            pool=pool,
                            tools_for_api=advertised_tools,
                            channel_id=channel_id,
                            parent_model=_agent_dispatch["model"] or "",
                            depth=0,
                            parent_watchdog=_agent_dispatch["watchdog"],
                        ),
                    )
                if name.startswith("mcp__"):
                    return await pool.call_tool(name, args)
                return await execute_builtin_tool(name, args, tool_ctx)

            # 7. API client
            # OAuth 二重アカウント: quota 起因 failover の復帰期限チェック
            # （非該当時は timestamp 比較のみの軽量パス）。
            if not restricted:
                try:
                    if await oauth_maybe_restore_primary():
                        await _safe_status(
                            status_callback, "✅ メインアカウントに復帰",
                        )
                except Exception as exc:
                    log.warning("oauth failback check failed: %s", exc)

            async def _try_failover(reason: str, retry_at: "float | None") -> bool:
                """アカウント切替（双方向）+ client 再構築。

                - active=primary で死亡 → failover へ（従来動作）
                - active=failover で死亡 → primary への強制切り戻しを試す
                  （2026-07-03 障害: failover の週次 quota 枯渇時に primary が
                  /login で復活済みでも全滅エラーになっていた片方向の穴）
                成功時は api_client と _agent_dispatch の両方を差し替える
                （既存 auth retry が dispatch を更新していなかったのは
                latent bug — こちらでは必ず両方更新する）。
                """
                nonlocal api_client
                if restricted:
                    return False
                if oauth_get_active_account() == "failover":
                    switched = await oauth_failback_to_primary(reason)
                    status_msg = "✅ サブ枠が使えないためメインアカウントに復帰"
                else:
                    switched = oauth_failover_to_secondary(
                        reason, primary_retry_at=retry_at,
                    )
                    status_msg = "⚠️ メイン枠が使えないためサブアカウントに切替"
                if not switched:
                    return False
                try:
                    api_client = await _make_api_client(restricted)
                except Exception as exc:
                    log.error("failover client rebuild failed: %s", exc)
                    return False
                _agent_dispatch["api_client"] = api_client
                await _safe_status(status_callback, status_msg)
                return True

            try:
                api_client = await _make_api_client(restricted)
            except OAuthRefreshFailed as exc:
                # primary の OAuth が恒久死（invalid_grant 等）— failover へ。
                log.error("primary OAuth dead at client build: %s", exc)
                if not restricted and oauth_failover_to_secondary("auth"):
                    api_client = await _make_api_client(restricted)
                    await _safe_status(
                        status_callback,
                        "⚠️ メイン認証が失効したためサブアカウントに切替",
                    )
                else:
                    return _error_message("auth"), None, resumable
            _agent_dispatch["api_client"] = api_client

            # 7b. History compaction (ADR-0010 phase 3): when the replay
            # window has outgrown the Fable-safe budget, fold the older
            # portion into a compact-summary row (official-CLI compact
            # semantics) and reload the collapsed window. Every failure mode
            # is non-fatal — the raw history still works via the refusal
            # fallback downstream, just at higher cost.
            if not restricted and prior_history:
                try:
                    # Token-only trigger (2026-06-17): the char-based cold-start
                    # fallback used to fire boundary compaction on the very first
                    # turn after a restart whenever the replayed transcript
                    # exceeded HISTORY_COMPACT_TRIGGER_CHARS (240K), which was
                    # absurd now that the 1M context beta is enabled and the
                    # token trigger sits at 500K — a restart was spending the
                    # whole first turn summarising a window that was nowhere
                    # near the real limit. We now gate purely on tokens. Cold-
                    # start (_last_tokens == 0) means no compaction this turn;
                    # the very next turn reports usage and the gate becomes
                    # authoritative. In-loop proactive compaction (ADR-0012)
                    # still catches a runaway turn mid-stream.
                    # Model-window-based trigger (CLI et6(K) port): fire near
                    # the effective window minus reserves, not a flat constant.
                    # HISTORY_COMPACT_PCT_OVERRIDE dials it (e.g. 50 = old 500K-
                    # on-1M behaviour). Bounds/floor are handled in the helper.
                    _compact_trigger = compact_threshold_tokens()
                    _last_tokens = get_last_context_tokens(channel_id)
                    over_tokens = _last_tokens > _compact_trigger
                    if over_tokens:
                        # Runtime opt (2026-07-08): the summary call takes
                        # minutes (measured 4.4-8.2 min at effort=max), so the
                        # boundary path no longer awaits it. Dispatch to a
                        # background task; THIS turn rides the raw window
                        # (immediate relief via the microcompact below) and the
                        # NEXT turn's normal replay load picks up the summary
                        # row. Guard against stale-usage thrash: after the
                        # summary lands, last_context_tokens stays high until a
                        # turn completes on the collapsed window — if the
                        # replay we just loaded is already small in chars, the
                        # token gate fired on stale usage and compacting again
                        # would summarize the summary.
                        from config import HISTORY_COMPACT_TRIGGER_CHARS
                        _replay_chars = estimate_replay_chars(prior_history)
                        if _replay_chars > HISTORY_COMPACT_TRIGGER_CHARS:
                            _spawned = start_background_compaction(
                                channel_id, api_client,
                            )
                            log.info(
                                "[ch=%d] boundary compaction trigger: "
                                "last_context_tokens=%d (trigger=%d) — "
                                "dispatched to background (spawned=%s)",
                                channel_id, _last_tokens, _compact_trigger,
                                _spawned,
                            )
                        else:
                            log.info(
                                "[ch=%d] boundary compaction skipped: token "
                                "gate is stale (tokens=%d > trigger=%d but "
                                "replay is only %d chars — already collapsed)",
                                channel_id, _last_tokens, _compact_trigger,
                                _replay_chars,
                            )
                            over_tokens = False
                    # Structure-safe, no-LLM relief for THIS turn while the
                    # summary runs in the background (and the only relief when
                    # the summary failed or the circuit breaker is open — the
                    # P1 2026-07-06 stall case). Clears OLD tool_result bodies
                    # (message order/roles/tool_use↔tool_result pairing
                    # preserved, so replay stays API-valid) so the tool-output
                    # bulk is reclaimed instead of riding into a TTFB stall.
                    if over_tokens and MICROCOMPACT_ENABLED:
                        prior_history, _emg_saved = microcompact_tool_results(
                            prior_history,
                            gap_minutes=MICROCOMPACT_GAP_MIN + 1.0,
                            gap_threshold_min=MICROCOMPACT_GAP_MIN,
                            keep_recent=MICROCOMPACT_KEEP_RECENT,
                            emergency=True,
                        )
                        if _emg_saved > 0:
                            log.info(
                                "[ch=%d] microcompact while boundary "
                                "compaction runs in background: ~%d tokens "
                                "reclaimed from old tool_results",
                                channel_id, _emg_saved,
                            )
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
            # 機能6: /model command can pre-seed a per-request override.
            forced_model: str | None = forced_model_override
            # Rapid-refill accounting for the in-loop proactive compaction
            # (audit #19): True when this request triggered one.
            proactive_fired = False

            # ADR-0040: checkpoint auto-resume policy. run_agent_loop に渡す
            # closure — checkpoint 発生時に True を返すと loop 側が「続けて」
            # 導線の代わりに自動継続ノートを出し、下の resume ループが実際に
            # 継続ターンを注入する。counter は resume ループと共有。
            from config import (
                CHECKPOINT_AUTO_RESUME_BUDGET_MAX,
                CHECKPOINT_AUTO_RESUME_ENABLED,
                CHECKPOINT_AUTO_RESUME_MAX,
            )
            _auto_resume_state = {"used": 0}

            def _should_auto_resume(reason: str) -> bool:
                if restricted or not CHECKPOINT_AUTO_RESUME_ENABLED:
                    return False
                cap = auto_resume_cap(
                    reason,
                    default_max=CHECKPOINT_AUTO_RESUME_MAX,
                    budget_max=CHECKPOINT_AUTO_RESUME_BUDGET_MAX,
                )
                return _auto_resume_state["used"] < cap

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

            # --- 熟考 P3: planner（deliberate tier のみ、retry を跨いで1回） ---
            # tools 無しの計画パス。trivial 判定で chat へ自己降格（過剰装備抑止）。
            # 失敗は plan 無しで続行（fail-open）。system prompt は不変。
            _plan_block: str | None = None
            if DELIB_PLANNER_ENABLED and tier == "deliberate":
                from config import DELIB_PLANNER_MODEL, DELIB_SCRATCHPAD_ENABLED
                from services.claude_api.planner import (
                    generate_plan,
                    render_plan_block,
                )
                await _safe_status(status_callback, "📋 計画中")
                _plan = await generate_plan(
                    user_request=raw_user_text or prompt,
                    model=(DELIB_PLANNER_MODEL or forced_model or MODEL),
                    channel_id=channel_id,
                )
                if _plan is not None and _plan.trivial:
                    log.info(
                        "[ch=%d] delib_planner self-deescalation → chat tier",
                        channel_id,
                    )
                    tier = "chat"
                elif _plan is not None:
                    _plan_block = render_plan_block(
                        _plan,
                        scratchpad_available=DELIB_SCRATCHPAD_ENABLED,
                    )

            # Phase 2 (ADR-0037): consume the post-compaction recovery flag
            # ONCE for this request (not per attempt), so every retry re-injects
            # the directive but the turn after compaction does not (no amnesia).
            _inject_recovery = (not restricted) and pop_pending_recovery(channel_id)

            for attempt in range(TRANSIENT_MAX_ATTEMPTS):
                if attempt > 0 and not check_and_record_retry_budget(channel_id):
                    log.warning("Retry budget exhausted for ch=%d", channel_id)
                    return _error_message("unknown"), None, False

                async with StallWatchdog(channel_id, status_callback) as watchdog:
                    _agent_dispatch["watchdog"] = watchdog
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
                        # 熟考 P3: 計画ブロックを user ターン先頭へ（指針であり
                        # 命令ではない。system prompt は cache 保護のため不変）。
                        if _plan_block:
                            user_content_for_loop = _prepend_context_snapshot(
                                user_content_for_loop, _plan_block,
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
                        # Phase 2 (ADR-0037): one-shot post-compaction recovery
                        # directive at the very front of the user turn (system
                        # stays byte-stable for cache reuse). Re-applied every
                        # attempt; the flag was already consumed above.
                        if _inject_recovery:
                            user_content_for_loop = _prepend_context_snapshot(
                                user_content_for_loop, RECOVERY_DIRECTIVE,
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
                            _get_sticky_model(channel_id, user_id) if not restricted else None
                        )
                        effective_model = forced_model or sticky_model or MODEL
                        # Agent tool subagent はこの effective_model を継承する。
                        _agent_dispatch["model"] = effective_model
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
                            scratch_stream_callback=scratch_stream_callback,
                            scratch_finalize_callback=scratch_finalize_callback,
                            thinking_stream_callback=thinking_stream_callback,
                            thinking_finalize_callback=thinking_finalize_callback,
                            progress=progress,
                            max_turns=max_turns,
                            budget_role="restricted" if restricted else "admin",  # ADR-0036
                            stall_watchdog=watchdog,
                            api_client=api_client,
                            model=effective_model,
                            extra_betas=MODEL_BETAS,
                            tool_search_enabled=(
                                TOOL_SEARCH_ENABLED and is_tool_search_active()
                            ),
                            evidence=evidence,
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
                            # 機能6: /effort command override (admin only).
                            output_config_override=(
                                {"effort": forced_effort_override}
                                if forced_effort_override else None
                            ),
                            # ADR-0040: checkpoint 時の自動継続ポリシー。
                            should_auto_resume=_should_auto_resume,
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
                                    channel_id, result.fallback_model, user_id
                                )
                        break
                    except asyncio.CancelledError as _cancel:
                        # ADR-0038/0045: the outer StallWatchdog cancels with
                        # "stream idle" — a 600s no-progress wedge that escaped
                        # every inner layer (byte 180 / event 300 / fallback
                        # 300). NOT a hung tool: those are deferred by
                        # tool_begin/tool_end and bounded by TOOL_CALL_TIMEOUT_S.
                        # Surface that as a timeout. A bare cancel is a real
                        # /abort or shutdown: re-raise so bot.py shows "⏹️ 中断"
                        # instead of mislabeling the user's own abort as a timeout.
                        if _cancel.args and _cancel.args[0] == "stream idle":
                            log.warning(
                                "[ch=%d] stream idle backstop → timeout",
                                channel_id,
                            )
                            return "⏰ タイムアウトしました", None, False
                        log.info(
                            "[ch=%d] agent loop cancelled (abort/shutdown)",
                            channel_id,
                        )
                        raise
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
                            clear_session(channel_id, user_id)
                            _clear_session_time(channel_id, user_id)
                            return _classified_error_message(category, exc), None, False
                        if category == "tool_use_mismatch":
                            # tool_use/tool_result pairing 400 (audit #14):
                            # the replayed transcript desynced. Recoverable —
                            # drop the session so the next turn reloads a
                            # clean, Yy8-swept replay window instead of
                            # treating the channel as permanently broken.
                            clear_session(channel_id, user_id)
                            _clear_session_time(channel_id, user_id)
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
                        if (
                            category == "invalid_request"
                            and TOOL_SEARCH_ENABLED
                            and is_tool_search_active()
                            and is_tool_search_request_error(exc)
                        ):
                            # Tool Search / defer_loading をこの API 経路
                            # (subscriber OAuth 等) が受け付けなかった。プロセス
                            # 寿命で auto-off し、従来の tools 構成に組み直して
                            # 同一リトライ予算内で即座に degrade する。
                            # invalid_request は通常 permanent 扱いで即 return
                            # されるため、この分岐がないと復帰できない。
                            disable_tool_search(f"API rejected tool search: {exc}")
                            advertised_tools = _build_tools()
                            await _safe_status(
                                status_callback,
                                "⚠️ ツール検索が使えないため従来モードに切替",
                            )
                            continue
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
                            _agent_dispatch["api_client"] = api_client
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
                            # auth (2nd time): reload でも直らない = 本当に
                            # credential が死んでいる → failover アカウントを試す。
                            if category == "auth" and await _try_failover(
                                "auth", None,
                            ):
                                continue
                            # max_turns: tool-loop hit the per-request turn
                            # cap — session history is still valid.
                            return _classified_error_message(category, exc), None, resumable
                        if not _is_transient(category):
                            # Permanent errors (api_exit, invalid_request,
                            # unknown, billing) — retrying won't help. Return
                            # immediately to avoid wasting API calls.
                            if category == "billing":
                                log.warning(
                                    "[ch=%d] billing/credit limit hit — not retrying",
                                    channel_id,
                                )
                                # subscriber quota の credit 系 429 — failover へ
                                if await _try_failover(
                                    "quota", _failover_retry_at(exc),
                                ):
                                    continue
                            return _classified_error_message(category, exc), None, resumable
                        # Transient (overloaded / rate_limit): the loop runs
                        # `range(TRANSIENT_MAX_ATTEMPTS)`, so the final attempt
                        # is `TRANSIENT_MAX_ATTEMPTS - 1`. We MUST return the
                        # correctly-classified error here on that attempt —
                        # otherwise the loop falls through with result=None and
                        # the function tail returns the "unknown" fallback
                        # ("内部エラー"), masking an overloaded as a generic
                        # failure (ADR-0006).
                        # OAuth 二重アカウント: subscriber 枠が構造的に尽きている
                        # シグナル（5h/7d バケットが rejected / utilization>=1.0）
                        # なら、backoff は数学的に全滅確定 — 即 failover。
                        if category == "rate_limit" and _quota_exhausted_signal():
                            if await _try_failover(
                                "quota", _failover_retry_at(exc),
                            ):
                                continue
                        if attempt >= TRANSIENT_MAX_ATTEMPTS - 1:
                            # Transient errors exhausted retries — 最後に一度
                            # だけ failover を試す（曖昧な 429 バーストが同一
                            # アカウントで解けなかったケースの受け皿）。
                            if category == "rate_limit" and await _try_failover(
                                "quota", _failover_retry_at(exc),
                            ):
                                continue
                            # keep the session alive so the next user message
                            # can resume without losing context.
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
                                # チャット応答としては待てない長さ = 実質 quota
                                # 切れ。give-up の前に failover を試す。
                                if category == "rate_limit" and await _try_failover(
                                    "quota", time.time() + retry_after,
                                ):
                                    continue
                                return _classified_error_message(category, exc), None, resumable
                            backoff = max(backoff, retry_after)
                        await _transient_backoff(
                            attempt, backoff, status_callback=status_callback,
                        )
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
                        await _transient_backoff(
                            attempt, compute_backoff(attempt),
                            status_callback=status_callback, label="接続再試行中",
                        )
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
                        await _transient_backoff(
                            attempt, compute_backoff(attempt),
                            status_callback=status_callback, label="接続再試行中",
                        )

            # --- ADR-0040: checkpoint auto-resume ------------------------------
            # graceful checkpoint (turn 上限 / 実行予算 / stream 停止) で止まった
            # リクエストを、ユーザーの「続けて」を待たずに自動継続する。revision
            # パイプラインと同じ「同一 history_for_loop に user ターンを注入して
            # もう1周」パターンなので、resume 分の turns も step 10 のスライス
            # （prior_len 起点）でそのまま JSONL に永続化される。tool_loop 起因は
            # ポリシー側（auto_resume_cap）で恒久除外。失敗時は checkpoint が
            # そのまま残るので、従来の手動「続けて」導線に自然に落ちる。
            if (
                result is not None
                and response_text is not None
                and getattr(result, "paused_reason", None)
                and result.synthetic_session_id is not None
                and not is_non_persistable_reply(response_text)
                and _should_auto_resume(result.paused_reason)
            ):
                from services.claude_api.tool_loop import AUTO_RESUME_DIRECTIVE
                _resume_segments: list[str] = []
                _resume_kw = dict(
                    history=history_for_loop,
                    system_prompt=system_prompt,
                    tools_for_api=advertised_tools,
                    can_use_tool=can_use_tool,
                    execute_tool=_execute_tool,
                    status_callback=status_callback,
                    incremental_text_callback=incremental_text_callback,
                    text_stream_callback=text_stream_callback,
                    tool_invocation_callback=tool_invocation_callback,
                    scratch_stream_callback=scratch_stream_callback,
                    scratch_finalize_callback=scratch_finalize_callback,
                    thinking_stream_callback=thinking_stream_callback,
                    thinking_finalize_callback=thinking_finalize_callback,
                    progress=progress,
                    max_turns=max_turns,
                    budget_role="admin",
                    api_client=api_client,
                    model=result.fallback_model or effective_model,
                    extra_betas=MODEL_BETAS,
                    active_mcp_keys=result.active_mcp_keys,
                    prior_turns=result.prior_len or 0,
                    tool_search_enabled=(
                        TOOL_SEARCH_ENABLED and is_tool_search_active()
                    ),
                    evidence=evidence,
                    proactive_compact=(
                        _proactive_compact if not restricted else None
                    ),
                    refusal_fallback_model=(
                        FABLE_REFUSAL_FALLBACK_MODEL
                        if (FABLE_REFUSAL_FALLBACK_ENABLED
                            and "fable" in (
                                (result.fallback_model or effective_model) or ""
                            ).lower())
                        else None
                    ),
                    output_config_override=(
                        {"effort": forced_effort_override}
                        if forced_effort_override else None
                    ),
                    should_auto_resume=_should_auto_resume,
                )
                while (
                    getattr(result, "paused_reason", None)
                    and result.synthetic_session_id is not None
                    and not is_non_persistable_reply(response_text or "")
                    and _should_auto_resume(result.paused_reason)
                ):
                    _auto_resume_state["used"] += 1
                    _n = _auto_resume_state["used"]
                    log.info(
                        "[ch=%d] checkpoint auto-resume %d: reason=%s",
                        channel_id, _n, result.paused_reason,
                    )
                    await _safe_status(
                        status_callback, f"⏳ 続きを自動実行中 ({_n})",
                    )
                    _resume_segments.append(strip_checkpoint_note(response_text))
                    try:
                        async with StallWatchdog(
                            channel_id, None, idle_timeout_s=600,
                        ) as _rwd:
                            r2 = await run_agent_loop(
                                AUTO_RESUME_DIRECTIVE, channel_id,
                                stall_watchdog=_rwd, **_resume_kw,
                            )
                    except Exception as exc:  # noqa: BLE001 — resume は best-effort
                        log.warning(
                            "[ch=%d] auto-resume failed (checkpoint stands): %s",
                            channel_id, exc,
                        )
                        _resume_segments.pop()
                        if incremental_text_callback is not None:
                            try:
                                await incremental_text_callback(
                                    "-# ⚠️ 自動継続に失敗しました。「続けて」で再開できます。"
                                )
                            except Exception:  # noqa: BLE001
                                pass
                        break
                    if (
                        not r2.synthetic_session_id
                        or not (r2.text or "").strip()
                        or is_non_persistable_reply(r2.text or "")
                    ):
                        # 再開ターンが sentinel / 空で返った — 直前の checkpoint を
                        # そのまま最終応答として残す（保存済みなので手動再開可能）。
                        log.warning(
                            "[ch=%d] auto-resume produced no usable reply "
                            "(session=%s) — keeping checkpoint",
                            channel_id, bool(r2.synthetic_session_id),
                        )
                        _resume_segments.pop()
                        if incremental_text_callback is not None:
                            try:
                                await incremental_text_callback(
                                    "-# ⚠️ 自動継続に失敗しました。「続けて」で再開できます。"
                                )
                            except Exception:  # noqa: BLE001
                                pass
                        break
                    # mid-resume の in-loop compaction が prior window を畳んだ
                    # 場合、prior_len は新しい boundary を指す — 次の resume と
                    # step 10 の永続化スライスをそこへ追従させる。
                    _resume_kw["prior_turns"] = r2.prior_len or 0
                    _resume_kw["active_mcp_keys"] = r2.active_mcp_keys
                    if r2.fallback_model:
                        _resume_kw["model"] = r2.fallback_model
                        if not restricted:
                            _record_sticky_model(
                                channel_id, r2.fallback_model, user_id,
                            )
                    result = r2
                    response_text = r2.text
                _kept_segments = [s for s in _resume_segments if s]
                if _kept_segments and (response_text or "").strip():
                    response_text = "\n\n".join(
                        _kept_segments + [response_text],
                    )

            # --- 熟考 P2/P5: deliberate tier の 検証・修正パイプライン --------
            # AsyncExitStack 内（revise の _execute_tool が pool を掴むため）。
            # クリーン終了（synthetic_session_id あり・非 sentinel 応答）のみ対象。
            # ADR-0040: checkpoint（paused_reason あり＝意図的に未完）で終わった
            # 応答は検証・修正の対象外 — 未完成な回答を「未完成だ」と指摘して
            # revision を回すのは無意味な追いコストで、caveat が checkpoint 文言を
            # 引用する珍妙な subtext（2026-07-08 の指摘）の原因だった。
            if (
                result is not None
                and response_text is not None
                and not restricted
                and tier == "deliberate"
                and result.synthetic_session_id is not None
                and not is_non_persistable_reply(response_text)
                and getattr(result, "paused_reason", None) is None
            ):
                _revise_kw = dict(
                    history=history_for_loop,
                    system_prompt=system_prompt,
                    tools_for_api=advertised_tools,
                    can_use_tool=can_use_tool,
                    execute_tool=_execute_tool,
                    status_callback=status_callback,
                    tool_invocation_callback=tool_invocation_callback,
                    thinking_stream_callback=thinking_stream_callback,
                    thinking_finalize_callback=thinking_finalize_callback,
                    max_turns=max_turns,
                    api_client=api_client,
                    model=effective_model,
                    extra_betas=MODEL_BETAS,
                    active_mcp_keys=result.active_mcp_keys,
                    prior_turns=result.prior_len or 0,
                    tool_search_enabled=(
                        TOOL_SEARCH_ENABLED and is_tool_search_active()
                    ),
                    evidence=evidence,
                    output_config_override=(
                        {"effort": forced_effort_override}
                        if forced_effort_override else None
                    ),
                )

                async def _run_revision(revise_prompt: str):
                    """verdict/lens ガイダンスを user ターンとして注入し 1 周。

                    履歴は同一 history_for_loop 継続なので、修正往復は step 10 の
                    スライス（prior_len 起点）でそのまま JSONL に永続化される。
                    """
                    async with StallWatchdog(
                        channel_id, None, idle_timeout_s=600,
                    ) as _vwd:
                        return await run_agent_loop(
                            revise_prompt, channel_id,
                            stall_watchdog=_vwd, **_revise_kw,
                        )

                # P5: MAGI-lite（判断系の問いのみ）— 3レンズ独立見解 + 合意度を
                # ガイダンスとして 1 回だけ反映する。失敗は無視（fail-open）。
                if MAGI_LITE_ENABLED and route_judgment and evidence is not None:
                    try:
                        from services.claude_api.magi_lite import (
                            build_lens_revise_prompt,
                            run_lenses,
                        )
                        await _safe_status(status_callback, "🧠 多視点で再考中")
                        magi = await run_lenses(
                            question=raw_user_text or prompt,
                            evidence_digest=evidence.digest(),
                            channel_id=channel_id,
                            model=effective_model,
                        )
                        if magi is not None and not magi.degraded:
                            r2 = await _run_revision(
                                build_lens_revise_prompt(magi))
                            if (
                                r2.synthetic_session_id
                                and r2.text.strip()
                                and not is_non_persistable_reply(r2.text)
                            ):
                                result = r2
                                response_text = r2.text
                                _revise_kw["active_mcp_keys"] = r2.active_mcp_keys
                    except Exception as exc:
                        log.warning(
                            "[ch=%d] magi_lite failed (non-fatal): %s",
                            channel_id, exc,
                        )

                # P2: verifier → revise（最大 DELIB_VERIFY_MAX_REVISIONS 回）→
                # まだ fail なら caveat を付けて正直に degrade。
                if DELIB_VERIFIER_ENABLED:
                    from services.claude_api.verifier import (
                        build_caveat,
                        build_revise_prompt,
                        verify_draft,
                    )
                    _v_model = DELIB_VERIFIER_MODEL or effective_model
                    _revisions = 0
                    while True:
                        await _safe_status(status_callback, "🔎 回答を検証中")
                        verdict = await verify_draft(
                            user_request=raw_user_text or prompt,
                            draft=response_text,
                            evidence_digest=(
                                evidence.digest() if evidence is not None
                                else "(no tool calls)"
                            ),
                            evidence_root=(
                                evidence.request_dir()
                                if evidence is not None else None
                            ),
                            api_client=api_client,
                            channel_id=channel_id,
                            model=_v_model,
                            extra_betas=MODEL_BETAS,
                            status_callback=status_callback,
                            thinking_stream_callback=verify_stream_callback,
                            thinking_finalize_callback=verify_finalize_callback,
                        )
                        if verdict.ok or verdict.degraded:
                            break
                        if _revisions >= DELIB_VERIFY_MAX_REVISIONS:
                            log.warning(
                                "[ch=%d] delib_verify still failing after %d "
                                "revision(s) → caveat degrade",
                                channel_id, _revisions,
                            )
                            response_text = (
                                response_text + "\n\n" + build_caveat(verdict)
                            )
                            break
                        _revisions += 1
                        await _safe_status(status_callback, "✍️ 指摘を反映して修正中")
                        try:
                            r2 = await _run_revision(build_revise_prompt(verdict))
                        except Exception as exc:
                            log.warning(
                                "[ch=%d] delib revise failed (keep draft): %s",
                                channel_id, exc,
                            )
                            break
                        if (
                            r2.synthetic_session_id
                            and r2.text.strip()
                            and not is_non_persistable_reply(r2.text)
                        ):
                            result = r2
                            response_text = r2.text
                        else:
                            break

            # judge-lite（chat tier の軽量ゲート）: tool を使った長文回答のみ。
            # streaming 済み本文は書き換えられないので caveat 追送だけ行う。
            if (
                result is not None
                and response_text is not None
                and DELIB_JUDGE_LITE_ENABLED
                and not is_non_persistable_reply(response_text)
                # ADR-0040: checkpoint された未完応答は judge の対象外（上の
                # deliberate パイプラインと同じ理由）。
                and getattr(result, "paused_reason", None) is None
            ):
                from services.claude_api.verifier import (
                    build_caveat as _jl_caveat,
                    judge_lite,
                    should_run_judge,
                )
                if should_run_judge(
                    response_text, evidence, tier=tier, restricted=restricted,
                ):
                    _jl = await judge_lite(
                        user_request=raw_user_text or prompt,
                        draft=response_text,
                        evidence_digest=(
                            evidence.digest() if evidence is not None else ""
                        ),
                        channel_id=channel_id,
                    )
                    if not _jl.ok and _jl.issues:
                        _cv = _jl_caveat(_jl)
                        if incremental_text_callback is not None:
                            try:
                                await incremental_text_callback(_cv)
                            except Exception:  # noqa: BLE001
                                pass
                        response_text = response_text + "\n\n" + _cv

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
            # 機能6: surface the turn's usage to /context.
            last_usage=getattr(result, "usage_total", None),
        )
        if result.synthetic_session_id:
            set_session(channel_id, user_id, session_info)
            _record_session_time(channel_id, user_id)

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

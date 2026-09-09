"""Discord bot — event handlers and message routing.

Matches free-code QueryEngine pattern: orchestrates services and utils.
"""

import asyncio
import ctypes
import ctypes.util
import gc
import io
import logging
import os
import re
import time
from collections import OrderedDict, deque
from types import SimpleNamespace

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import bot_commands
import bot_preprocess
import bot_record
import bot_ui
from bot_pipeline import admission as bot_admission
from bot_pipeline import context as bot_context
from bot_pipeline import model as bot_model
from bot_pipeline import prompt as bot_prompt
from bot_pipeline import publish as bot_publish
from bot_pipeline import record as bot_record_stage
from bot_pipeline.fifo import AdmissionTicket, FifoAdmission
from bot_pipeline.runtime import RuntimeResources
from bot_pipeline.turn_outbox import TurnOutbox

from config import (
    ADMIN_USER_IDS,
    CLAUDE_TAG_EMOJI_ID,
    CLAUDE_TAG_EMOJI_NAME,
    CLAUDE_TAG_ENABLED,
    DEEP_RESEARCH_ENABLED,
    DELIB_ROUTER_ENABLED,
    SPINNER_MODE,
    BOT_IGNORED_ROLE_IDS,
    BOT_KEYWORDS,
    BOT_ROLE_IDS,
    COOLDOWN_SECONDS,
    DISCORD_TOKEN,
    MAX_CHANNEL_SEMAS,
    MAX_COOLDOWN_ENTRIES,
    MAX_PROMPT_CHARS,
    MAX_USER_MESSAGE_CHARS,
    MATH_PRESENTATION_MODE,
    MEM0_AUTO_RECORD_ENABLED,
    MEM0_AUTO_RECORD_INTERVAL_S,
    MEM0_AUTO_RECORD_MIN_CHARS,
    MEM0_HISTORY_EMBED_ENABLED,
    MEM0_HISTORY_EMBED_LIMIT,
    MEM0_HISTORY_SEARCH_ENABLED,
    MEM0_HISTORY_SEARCH_LIMIT,
    MEM0_LEGACY_CONTEXT,
    MEM0_MIN_QUERY_LENGTH,
    MEM0_RECALL_LIMIT,
    MEM0_RECALL_LIMIT_RESTRICTED,
    MEM0_SMART_SEARCH,
    PLAN_STREAM_ENABLED,
    RESTRICTED_COOLDOWN_SECONDS,
    RESTRICTED_DAILY_QUOTA,
    SCRATCHPAD_STREAM_ENABLED,
    SHUTDOWN_DRAIN_TIMEOUT_S,
    SHUTDOWN_FORCE_CLEANUP_TIMEOUT_S,
    SLEEP_ENFORCE_ENABLED,
    SLEEP_ENFORCE_END_HOUR,
    SLEEP_ENFORCE_START_HOUR,
    SLEEP_ENFORCE_USER_ID,
    STATE_DIR,
    speaker_label,
)
from services.claude_api import _is_session_fresh, kill_orphan_claude_cli
from services.codex_app_server import (
    CodexAuthError,
    CodexProtocolError,
    CodexTurnError,
    CodexUnavailableError,
)
from services.provider_router import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    call_with_provider,
    provider_label,
    provider_resume_hint,
    resolve_provider,
)
from services.skills import SkillError, render_active_skill_prompt

from services.oauth_manager import OAuthRefreshFailed
from services import claude_tag
from services import deep_research
from services import r2_archive
from services.mem0 import (
    add_memories,
    delete_memory,
    format_history_for_prompt,
    format_memories_for_prompt,
    get_all_memories,
    search_memories,
    search_memories_smart,
    should_use_memory_llm_mode,
)
from services.session import (
    SessionInfo,
    clear_channel_context,
    get_session,
    load_history_channel,
    shared_sessions,
    update_session,
)
from services.prompt_builder import PromptBuilder
from utils.attachments import (
    IMAGE_CONTENT_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ARBITRARY_ATTACHMENT_BYTES,
    MAX_TEXT_ATTACHMENT_BYTES,
    TEXT_CONTENT_TYPES,
    attachment_metadata_by_path,
    cleanup_attachments,
    cleanup_stale_attachments,
    download_arbitrary_attachments,
    download_attachments,
    download_text_attachments,
    extract_file_paths,
    extract_image_paths,
    is_attach_ignored,
    normalize_image_filenames,
    strip_attached_paths,
)
from utils.browser_pool import close_browser_daemon
from utils.discord_helpers import (
    ReplyChainSender,
    build_mention_regex,
    fetch_channel_context,
    fetch_thread_context,
    resolve_channel_mention_contexts,
    resolve_message_links,
    split_message,
    strip_mention,
)
from utils.markdown_parser import has_math, has_tables, mask_code_blocks, parse_segments
from utils.math_presentation import (
    classify_math,
    latex_to_discord_text,
    plan_segments,
)
from utils.math_renderer import render_math_image
from utils.response_sanitizer import strip_numeric_inline_code
from utils.table_renderer import render_table_image

# Compatibility seam for the existing characterization suite and out-of-tree
# integrations that patch ``bot.call_with_tools``. The implementation is now
# provider-neutral, but retaining this module attribute avoids a flag-day
# migration for callers that replace the model boundary in tests.
call_with_tools = call_with_provider

log = logging.getLogger(__name__)


def _classify_cancellation(exc: asyncio.CancelledError) -> tuple[str, str]:
    """Return the exact cancellation reason and its stable operational class."""
    reason = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
    kinds = {
        "user abort": "user_abort",
        "shutdown": "shutdown",
        "stream idle": "stream_idle",
    }
    return reason, kinds.get(reason, "unexpected")


# ---------------------------------------------------------------------------
# Mem0 auto-record throttle (token-reduction plan, 2026-05-15).
# Default behaviour used to write every admin turn into Mem0, which churned
# the ingestion pipeline on short follow-ups. We now record only when the
# user message is substantial AND enough time has passed since the last
# record on the same channel. Explicit "覚えて" / "remember this" bypasses
# the throttle so an intentional capture is never dropped.
# ---------------------------------------------------------------------------
_last_mem0_record_at = bot_record_stage._last_mem0_record_at
_MEM0_FORCE_RECORD_RE = bot_record_stage._MEM0_FORCE_RECORD_RE


def _is_force_record_request(user_text: str) -> bool:
    return bot_record_stage.is_force_record_request(user_text)


def _should_auto_record_mem0(
    channel_id: int,
    user_text: str,
    response_text: str = "",
) -> bool:
    """Decide whether to fire the background Mem0 store for this turn.

    Uses ``max(len(user_text), len(response_text))`` so a short user
    input that triggers a long analytical response still gets recorded.
    """
    return bot_record_stage.should_auto_record_mem0(
        channel_id,
        user_text,
        response_text,
        enabled=MEM0_AUTO_RECORD_ENABLED,
        min_chars=MEM0_AUTO_RECORD_MIN_CHARS,
        interval_s=MEM0_AUTO_RECORD_INTERVAL_S,
    )


def _mark_mem0_recorded(channel_id: int) -> None:
    bot_record_stage.mark_mem0_recorded(channel_id)


# Shared rate-limit ledger (P2.5 group-1): bot.py's local cooldown/quota
# fallback duplicated services.rate_limit behind a defensive import that
# could silently split the ledger again. The shared module is a hard dep.
from services.rate_limit import (
    check_cooldown as _rl_check_cooldown,
    check_daily_quota as _rl_check_daily_quota,
    daily_quota_reset_seconds as _rl_daily_quota_reset_seconds,
)

from services.job_state import (
    JobPhase,
    JobState,
    cancel_channel_jobs,
    live_job_tasks,
    register_job,
    unregister_job,
)


def _fmt_clock(epoch_s: float) -> str:
    """HH:MM local-time label for '解除目安' style user-facing wording."""
    return bot_admission.fmt_clock(epoch_s)


def _mb(n: int) -> str:
    return f"{n // (1024 * 1024)}MB"


def _classify_attachment_rejections(
    attachments: list,
    *,
    caller_is_admin: bool,
) -> tuple[int, list[tuple[str, str]]]:
    """Predict which attachments the downloaders will silently drop (P2).

    Mirrors the size / type gates in utils.attachments from each attachment's
    metadata so the user gets a reason instead of a silent drop. Returns
    ``(accepted_count, [(filename, reason), ...])``. Best-effort: a file
    counted "accepted" here can still fail mid-download (logged separately),
    but the common rejections (oversize, unsupported type, non-admin) are
    surfaced.
    """
    return bot_admission.classify_attachment_rejections(
        attachments,
        caller_is_admin=caller_is_admin,
        limits=bot_admission.AttachmentLimits(
            image_content_types=IMAGE_CONTENT_TYPES,
            text_content_types=TEXT_CONTENT_TYPES,
            max_image_bytes=MAX_ATTACHMENT_BYTES,
            max_text_bytes=MAX_TEXT_ATTACHMENT_BYTES,
            max_arbitrary_bytes=MAX_ARBITRARY_ATTACHMENT_BYTES,
        ),
    )


# ---------------------------------------------------------------------------
# Correlation / request-ID module (SRE-H4) — defensive import with no-op fallback
# ---------------------------------------------------------------------------
try:
    from services.correlation import new_request_id, get_request_id
except ImportError:
    import uuid

    def new_request_id() -> str:  # type: ignore[misc]
        return ""

    def get_request_id() -> str:  # type: ignore[misc]
        return ""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
sessions = shared_sessions()
# 機能6: /model and /effort live overrides, keyed per (channel, user). Set by
# the slash commands, consumed by call_with_tools below. In-memory only — a
# restart clears overrides back to the env defaults intentionally; the commands
# are cheap to re-issue.
_MODEL_OVERRIDES: dict[tuple[int, int], str] = {}
_EFFORT_OVERRIDES: dict[tuple[int, int], str] = {}
_PROVIDER_OVERRIDES: dict[tuple[int, int], str] = {}
_SKILL_OVERRIDES: dict[tuple[int, int], object] = {}
# /tier live override（2026-07-08）: "deliberate"（熟考固定）| "chat"（会話固定）。
# 未設定ならルーター判定（既定は明示マーカーのみ — DELIB_ROUTER_AUTO_ENABLED
# が false の間、スコアによる自動昇格はしない）。
_TIER_OVERRIDES: dict[tuple[int, int], str] = {}
# SRE-H1: gate bot.tree.sync() to once per process (not per Discord reconnect).
_tree_synced: bool = False
# LRU-bounded: evict least-recently-used channel sema when over MAX_CHANNEL_SEMAS.
_channel_semas: OrderedDict[int, FifoAdmission] = OrderedDict()
_http_session: aiohttp.ClientSession | None = None

# Tracking set for fire-and-forget background tasks. Without this, tasks
# created with bare `asyncio.create_task(coro)` get GC'd at random and
# their exceptions surface as `Task exception was never retrieved`. The
# set keeps a strong reference until the task finishes; the done callback
# both removes it and logs any unhandled exception so failures don't get
# silently swallowed.
_background_tasks: set[asyncio.Task] = set()

# Phase G/D5: one-shot guard so the on_ready cleanup doesn't fire again on
# every Discord reconnect (which is otherwise harmless but wasteful).
_cleanup_scheduled: bool = False

# Job lifecycle tracking moved to services/job_state.py: jobs register BEFORE
# the channel semaphore, so /abort reaches queued work too, and the queue
# position can be shown to the user.


def _track_task(coro, *, name: str | None = None) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine and track it for GC + error logging."""
    task = _runtime_resources.create(coro, name=name)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        log.warning(
            "Background task %r failed: %s",
            task.get_name(),
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


_runtime_resources = RuntimeResources(
    background_tasks=_background_tasks,
    close_browser=lambda: close_browser_daemon(),
    active_tasks=live_job_tasks,
    drain_timeout_s=SHUTDOWN_DRAIN_TIMEOUT_S,
    force_cancel_timeout_s=SHUTDOWN_FORCE_CLEANUP_TIMEOUT_S,
)
_turn_outbox = TurnOutbox(os.path.join(STATE_DIR, "bot_turn_outbox.json"))


def _serialize_session_info(info) -> dict | str | None:
    if info is None or isinstance(info, str):
        return info
    return {
        "session_id": info.session_id,
        "mcp_server_keys": list(getattr(info, "mcp_server_keys", [])),
        "last_usage": getattr(info, "last_usage", None),
    }


def _deserialize_session_info(value):
    if value is None or isinstance(value, str):
        return value
    return SessionInfo(
        session_id=value["session_id"],
        mcp_server_keys=list(value.get("mcp_server_keys", [])),
        last_usage=value.get("last_usage"),
    )


async def _reconcile_turn_outbox(request_id: str) -> bool:
    async def archive_step(job: dict) -> None:
        archive_kwargs = dict(job["kwargs"])
        archive_kwargs["idempotency_key"] = job["idempotency_key"]
        refs = await asyncio.to_thread(
            r2_archive.archive_images,
            job["paths"],
            **archive_kwargs,
        )
        if (
            refs
            and job.get("bind_response", True)
            and job.get("response_message_id") is not None
        ):
            await asyncio.to_thread(
                r2_archive.bind_response_message,
                refs,
                job["response_message_id"],
            )

    async def session_step(turn: dict) -> None:
        payload = turn["payload"]
        info = _deserialize_session_info(payload.get("session_info"))
        if info is None:
            return
        await asyncio.to_thread(
            update_session,
            sessions,
            turn["channel_id"],
            info,
            turn["user_id"],
        )

    async def record_step(turn: dict) -> None:
        payload = turn["payload"]
        if not payload.get("caller_is_admin"):
            return
        channel = SimpleNamespace(
            id=turn["channel_id"],
            name=payload.get("channel_name", "DM"),
        )
        replay_message = SimpleNamespace(
            id=turn["request_message_id"],
            channel=channel,
            author=SimpleNamespace(id=turn["user_id"]),
        )
        await bot_record_stage.record_admin_turn(
            caller_is_admin=True,
            message=replay_message,
            raw_user_text=payload["raw_user_text"],
            response_text=payload["response_text"],
            mem0_user_id=payload["mem0_user_id"],
            conv_context=payload.get("conv_context", ""),
            record_turn=bot_record.record_turn,
            add_memories=add_memories,
            track_task=_track_task,
            load_history_channel=load_history_channel,
            is_force_record_request_hook=_is_force_record_request,
            request_id=turn["idempotency_key"],
        )

    return await _turn_outbox.reconcile(
        request_id,
        archive=archive_step,
        session=session_step,
        record=record_step,
    )


async def _replay_turn_outbox() -> None:
    for turn in await _turn_outbox.run_io(_turn_outbox.pending):
        await _reconcile_turn_outbox(turn["request_id"])


def _get_channel_sema(channel_id: int) -> FifoAdmission:
    sema = _channel_semas.get(channel_id)
    if sema is None:
        sema = FifoAdmission()  # one FIFO-reserved request per channel at a time
        _channel_semas[channel_id] = sema
    else:
        _channel_semas.move_to_end(channel_id)
    # Evict idle (not currently locked) LRU entries when over limit. The
    # previous version broke out of the loop on the first locked entry,
    # which meant a head full of locked semas pinned the dict above its
    # cap and never evicted idle entries further back. Walk a snapshot
    # of keys and stop only after `over` entries have been removed.
    if len(_channel_semas) > MAX_CHANNEL_SEMAS:
        over = len(_channel_semas) - MAX_CHANNEL_SEMAS
        evicted = 0
        for oldest_id in list(_channel_semas.keys()):
            if evicted >= over:
                break
            if oldest_id == channel_id:
                continue
            oldest_sema = _channel_semas[oldest_id]
            if oldest_sema.locked():
                continue
            _channel_semas.pop(oldest_id, None)
            evicted += 1
    return sema


log.info("Loaded %d sessions from disk", len(sessions))


# ---------------------------------------------------------------------------
# Prompt-injection sanitization (C-2)
# ---------------------------------------------------------------------------
# Regexes and the sanitize function live in services/sentinel.py as the
# single source of truth — services/prompt_builder.py uses the same module.
# Locally-named aliases preserve back-compat for call sites that still spell
# them with the underscore prefix.
from services.sentinel import (
    SENTINEL_RE as _SENTINEL_RE,
    POWER_PHRASE_RE as _POWER_PHRASE_RE,
    sanitize_untrusted_text as _sanitize_untrusted_text,
)


def _wrap_untrusted(text: str) -> str:
    """Wrap untrusted text in an XML tag the model is told to treat as data."""
    return bot_prompt.wrap_untrusted(text)


def _wrap_trusted(text: str) -> str:
    """Wrap admin-authored text in a trusted tag.

    Admin requests are first-party instructions, not data to be filtered.
    Using a distinct tag (vs. the untrusted wrapper) makes the trust level
    explicit to the model and stops it from second-guessing legitimate
    admin commands as "external input".
    """
    return bot_prompt.wrap_trusted(text)


def _wrap_user_input(text: str, *, is_admin: bool) -> str:
    """Pick the right wrapper based on the sender's admin status."""
    return bot_prompt.wrap_user_input(text, is_admin=is_admin)


_JP_STOP_WORDS = frozenset(
    "の は が を に で と も か な へ や て だ です ます した して "
    "する ある いる なる れる られる せる させる ない よう から まで "
    "こと もの ため ので のに けど けれど って という といった "
    "これ それ あれ ここ そこ あそこ この その あの どの "
    "私 僕 俺 あなた 彼 彼女 先輩 さん ちゃん くん".split()
)
# P3: _build_mem0_queries / _EN_STOP_WORDS は services.prompt_builder.PromptBuilder.build_mem0_queries に集約済 (重複削除 2026-05-12)


def _cap_text(text: str, limit: int) -> str:
    return bot_prompt.cap_text(text, limit)


# ---------------------------------------------------------------------------
# Daily quota tracking for non-admin callers (M-4)
# ---------------------------------------------------------------------------
async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


def is_admin(user: discord.User | discord.Member) -> bool:
    return user.id in ADMIN_USER_IDS


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
# `max_messages` defaults to 1000 in discord.py — that means the client
# keeps the last 1000 messages from every channel/guild it sees in RAM.
# 200 is more than enough for `message.reference.cached_message` lookups,
# and trims tens of MB off the resident set on long-running instances.
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    max_messages=200,
    # Default to suppressing @everyone / @here / @role pings on every outgoing
    # message. Echoing user-controlled text that contains "@everyone" should
    # not page the whole server. Per-message overrides can still pass
    # allowed_mentions=AllowedMentions(users=True) when explicitly intended.
    allowed_mentions=discord.AllowedMentions.none(),
)


# ---------------------------------------------------------------------------
# Lifecycle (free-code: graceful shutdown)
# ---------------------------------------------------------------------------
_orig_close = bot.close


def _begin_drain() -> bool:
    transitioned = _runtime_resources.begin_drain()
    if transitioned:
        log.warning(
            "Discord job drain started (active_jobs=%d, drain_timeout_s=%d, "
            "force_cleanup_timeout_s=%d)",
            _runtime_resources.active_job_count,
            SHUTDOWN_DRAIN_TIMEOUT_S,
            SHUTDOWN_FORCE_CLEANUP_TIMEOUT_S,
        )
    return transitioned


async def _patched_close() -> None:
    global _http_session
    log.info("Closing bot and cleaning up resources...")
    _begin_drain()
    try:
        await _runtime_resources.close()
    finally:
        try:
            if _http_session and not _http_session.closed:
                await _http_session.close()
                _http_session = None
        finally:
            await _orig_close()


bot.close = _patched_close
bot.begin_drain = _begin_drain
bot.accepting_jobs = lambda: _runtime_resources.accepting_jobs


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Memory hygiene (ADR-0037 follow-up). glibc frees large transient allocations
# (PIL image processing, big tool-loop message lists loaded for the API call)
# into its arenas but does NOT return them to the OS, so RSS creeps over a long
# uptime (measured 181→238MB / 37d, no OOM). malloc_trim(0) forces glibc to
# hand the free arena top back; gc.freeze() (in on_ready) moves the static
# startup heap out of gc's scan set so collections stay cheap and don't churn.
# ---------------------------------------------------------------------------
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
except Exception:  # pragma: no cover — non-glibc / musl: no malloc_trim
    _libc = None


def _malloc_trim() -> None:
    """Return glibc free-arena memory to the OS. No-op off glibc."""
    if _libc is None:
        return
    try:
        _libc.malloc_trim(0)
    except Exception:  # noqa: BLE001 — best-effort, never fatal
        pass


async def _periodic_memory_trim(interval_s: float = 300.0) -> None:
    """Every ``interval_s`` reclaim post-spike memory: collect cyclic garbage,
    then hand free arenas back to the OS. Cheap after gc.freeze() — only the
    young (non-frozen) generation is scanned."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            gc.collect()
            _malloc_trim()
        except Exception:  # noqa: BLE001
            pass


@bot.event
async def on_ready():
    for guild in bot.guilds:
        member = guild.get_member(bot.user.id)
        if member:
            for role in member.roles:
                if not role.is_default():
                    BOT_ROLE_IDS.add(role.id)
    build_mention_regex(bot.user.id)
    # Sync slash commands with Discord (SRE-H1: only once per process,
    # not on every reconnect, to avoid the global-sync rate limit).
    global _tree_synced
    if not _tree_synced:
        try:
            synced = await bot.tree.sync()
            log.info("Synced %d slash command(s)", len(synced))
            _tree_synced = True
        except Exception as e:
            log.error("Failed to sync slash commands: %s", e)
    else:
        log.debug("Slash command sync already done; skipping on reconnect")
    log.info(
        "Bot is ready: %s (id=%s), role_ids=%s", bot.user, bot.user.id, BOT_ROLE_IDS
    )
    # Sanity-check that the env-pinned kanae namespace matches the live
    # bot id. Mismatch means Mem0 writes land in a different collection.
    # Just a warning — don't refuse to start.
    from config import KANAE_BOT_USER_ID

    if KANAE_BOT_USER_ID and KANAE_BOT_USER_ID != str(bot.user.id):
        log.warning(
            "KANAE_BOT_USER_ID=%s does not match live bot.user.id=%s; "
            "daily summaries will write to a different Mem0 namespace.",
            KANAE_BOT_USER_ID,
            bot.user.id,
        )
    # claude tag (ADR-0027): warn if the trigger emoji isn't pinned by ID, so a
    # same-named :claude: from another guild can't misfire the task path.
    if CLAUDE_TAG_ENABLED and CLAUDE_TAG_EMOJI_ID is None:
        log.warning(
            "CLAUDE_TAG_ENABLED but CLAUDE_TAG_EMOJI_ID is unset; :claude: "
            "reactions will fire on emoji NAME match alone (%r), which can "
            "misfire from another guild's same-named emoji. Set "
            "CLAUDE_TAG_EMOJI_ID in .env to pin the exact emoji.",
            CLAUDE_TAG_EMOJI_NAME,
        )
    # Deep Research (ADR-0028): /research needs a general web backend.
    if DEEP_RESEARCH_ENABLED and not deep_research.has_required_web_backend():
        log.warning(
            "DEEP_RESEARCH_ENABLED but no general web backend (brave-search / "
            "tavily) is configured; /research will refuse. Set BRAVE_API_KEY or "
            "TAVILY_API_KEY in .env."
        )
    # D5: cleanup_stale_attachments was previously called synchronously from
    # main.py before bot.run(). For a tmpdir with thousands of files the
    # scan+stat loop blocked startup. Schedule it as a fire-and-forget
    # background task here so the bot is reachable while it runs. on_ready
    # fires on every reconnect; the _cleanup_scheduled guard ensures we
    # only run cleanup once per process lifetime.
    global _cleanup_scheduled
    if not _cleanup_scheduled:
        _cleanup_scheduled = True
        _track_task(
            asyncio.to_thread(cleanup_stale_attachments),
            name="cleanup_stale_attachments",
        )
        # Memory hygiene (ADR-0037 follow-up): freeze the static startup heap so
        # gc stops rescanning it, then start the periodic OS-return trim.
        gc.collect()
        gc.freeze()
        _track_task(_periodic_memory_trim(), name="memory_trim")
        _track_task(r2_archive.archive_worker(), name="r2_archive")
        _track_task(_replay_turn_outbox(), name="turn_outbox_replay")


# ---------------------------------------------------------------------------
# Segmented response sender (table images + text)
# ---------------------------------------------------------------------------
def _log_math_presentation(segments) -> None:
    bot_publish.log_math_presentation(
        segments,
        math_presentation_mode=MATH_PRESENTATION_MODE,
        classify_math=classify_math,
    )


async def _send_segmented(
    message: discord.Message,
    response_text: str,
    attach_files: list[discord.File],
    *,
    delivery_state: bot_publish.DeliveryState | None = None,
    on_first_ack=None,
    register_archive=None,
) -> int | None:
    return await bot_publish.send_segmented(
        message,
        response_text,
        attach_files,
        bot_user=bot.user,
        math_presentation_mode=MATH_PRESENTATION_MODE,
        deps=bot_publish.SegmentedDeps(
            parse_segments=parse_segments,
            plan_segments=plan_segments,
            classify_math=classify_math,
            latex_to_discord_text=latex_to_discord_text,
            render_table_image=render_table_image,
            render_math_image=render_math_image,
            reply_chain_sender=ReplyChainSender,
            archive_images=r2_archive.archive_images,
            split_message=split_message,
        ),
        delivery_state=delivery_state,
        on_first_ack=on_first_ack,
        register_archive=register_archive,
    )


# ---------------------------------------------------------------------------
# claude tag — :claude: リアクションでタスクをスレッド処理 (ADR-0027)
# ---------------------------------------------------------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Dispatch a :claude: reaction to the task-processing orchestrator.

    Only the cheap, side-effect-free gate runs inline on every reaction; all
    heavy work is delegated to services.claude_tag as a tracked fire-and-forget
    task so the gateway loop is never blocked. on_message is left untouched —
    the orchestrator shares _get_channel_sema / sessions / _send_segmented via
    the injected TagDeps, which is what serializes it against on_message.
    """
    if not CLAUDE_TAG_ENABLED or bot.user is None:
        return
    if not claude_tag.passes_cheap_gate(
        payload,
        bot_user_id=bot.user.id,
        admin_ids=ADMIN_USER_IDS,
        emoji_id=CLAUDE_TAG_EMOJI_ID,
        emoji_name=CLAUDE_TAG_EMOJI_NAME,
    ):
        return
    deps = claude_tag.TagDeps(
        bot=bot,
        sessions=sessions,
        send_segmented=_send_segmented,
        get_channel_sema=_get_channel_sema,
        get_http_session=get_http_session,
        track_task=_track_task,
    )
    _track_task(
        claude_tag.handle_claude_tag(payload, deps),
        name="claude_tag",
    )


# ---------------------------------------------------------------------------
# Slash commands — moved to bot_commands/ (P2-d). register_all runs at import
# time, matching the old decorator-at-module-level behavior; tree.sync timing
# stays owned by on_ready.
# ---------------------------------------------------------------------------
# Kept as a module attribute so tests can patch per-command dependencies
# (e.g. patch.object(bot._command_deps, "track_task", ...)).
_command_deps = bot_commands.CommandDeps(
    bot=bot,
    is_admin=is_admin,
    track_task=_track_task,
    send_segmented=_send_segmented,
    get_http_session=get_http_session,
    model_overrides=_MODEL_OVERRIDES,
    effort_overrides=_EFFORT_OVERRIDES,
    tier_overrides=_TIER_OVERRIDES,
    get_channel_sema=_get_channel_sema,
    provider_overrides=_PROVIDER_OVERRIDES,
    skill_overrides=_SKILL_OVERRIDES,
)
bot_commands.register_all(bot.tree, _command_deps)


async def _index_channel_message(message: discord.Message) -> None:
    """Persist + index a non-mention message for later literal keyword recall.

    Stored via append_jsonl with role="channel" so replay / conv_context
    ignore it (they only read user/assistant rows), while the append_jsonl
    hook mirrors it into the history BM25 store. Durable, so a reindex
    rebuilds it. Fire-and-forget; never blocks message handling.
    """
    from services.session import append_jsonl

    await bot_record_stage.index_channel_message(
        message,
        cap_text=_cap_text,
        max_user_message_chars=MAX_USER_MESSAGE_CHARS,
        append_jsonl=append_jsonl,
    )


def _reply_ping_addresses_bot(
    message: discord.Message,
    *,
    bot_user_id: int,
) -> bool:
    """Return whether Discord's reply ping targets this bot identity.

    A reply with ``mention_author=True`` adds the referenced author to the
    gateway payload's ``mentions`` array, but does not add a literal
    ``<@user_id>`` token to ``content``.  Checking ``mentions`` alone is not
    safe because role mentions can expand into the same array.  Requiring the
    referenced message author to be this exact bot distinguishes a reply ping
    from a role expansion and keeps co-located bot identities isolated.
    """
    return bot_admission.reply_ping_addresses_bot(
        message,
        bot_user_id=bot_user_id,
        forward_reference_type=discord.MessageReferenceType.forward,
    )


def _guild_message_addresses_bot(message: discord.Message) -> bool:
    """Return whether a guild message explicitly addresses this bot process.

    Discord may include users reached through a role mention in
    ``message.mentions``. Treating membership in that expanded list as a
    direct mention makes two bot identities answer one role callout. Direct
    user mentions are therefore determined from the literal ``<@id>`` token,
    except for a reply ping whose referenced author is this exact bot.
    """
    return bot_admission.guild_message_addresses_bot(
        message,
        bot_user=bot.user,
        ignored_role_ids=BOT_IGNORED_ROLE_IDS,
        bot_role_ids=BOT_ROLE_IDS,
        bot_keywords=BOT_KEYWORDS,
        forward_reference_type=discord.MessageReferenceType.forward,
    )


@bot.event
async def on_raw_message_delete(payload):
    from services import guild_context
    await guild_context.forget([payload.message_id])


@bot.event
async def on_raw_bulk_message_delete(payload):
    from services import guild_context
    await guild_context.forget(list(payload.message_ids))


@bot.event
async def on_raw_message_edit(payload):
    from services import guild_context
    await guild_context.observe_edit(payload)


@bot.event
async def on_message(message: discord.Message):
    # Own the dispatch before admission awaits or spawns indexing work. This
    # closes the phase gap where a SIGTERM could otherwise miss a task that had
    # not registered JobState yet, or had marked it terminal before its finally
    # cleanup completed.
    if not _runtime_resources.register_dispatch_task(asyncio.current_task()):
        return
    from services import guild_context
    # Receipt precedes admission: a correction addressed to another speaker
    # must still be visible to subsequent turns, without making the bot reply.
    await guild_context.observe(message)
    admitted = await bot_admission.admit_message(
        message,
        bot=bot,
        dm_channel_types=(discord.DMChannel, discord.GroupChannel),
        deps=bot_admission.AdmissionDeps(
            is_admin=is_admin,
            is_active_research_thread=deep_research.is_active_research_thread,
            new_request_id=new_request_id,
            get_request_id=get_request_id,
            guild_message_addresses_bot=_guild_message_addresses_bot,
            track_channel_message=lambda item: _track_task(
                _index_channel_message(item),
                name="channel_index",
            ),
            handle_prefix_commands=bot_commands.prefix.handle_prefix_commands,
            strip_mention=strip_mention,
            check_cooldown=_rl_check_cooldown,
            check_daily_quota=_rl_check_daily_quota,
            daily_quota_reset_seconds=_rl_daily_quota_reset_seconds,
            cap_text=_cap_text,
            history_search_enabled=MEM0_HISTORY_SEARCH_ENABLED,
            max_user_message_chars=MAX_USER_MESSAGE_CHARS,
            fmt_clock=_fmt_clock,
        ),
    )
    if admitted is None:
        return
    if not _runtime_resources.accepting_jobs:
        try:
            await message.channel.send(
                "サービスを再起動しています。起動完了後に再送してください。"
            )
        except Exception:
            pass
        return
    caller_is_admin = admitted.caller_is_admin
    is_dm = admitted.is_dm
    raw_user_text = admitted.raw_user_text
    prompt = raw_user_text

    _request_key = (message.channel.id, message.author.id)
    _forced_model = _MODEL_OVERRIDES.get(_request_key)
    _forced_effort = _EFFORT_OVERRIDES.get(_request_key)
    _provider = resolve_provider(
        explicit=_PROVIDER_OVERRIDES.get(_request_key),
        model=_forced_model,
        restricted=not caller_is_admin,
    )

    sema = _get_channel_sema(message.channel.id)

    # P0 job lifecycle: register BEFORE the semaphore so queued work is
    # visible (/abort can cancel it) and the user gets an immediate status
    # reply — queue position while waiting, instead of emoji reactions.
    turn_request_id = get_request_id() or f"discord-{message.id}"
    job = JobState(
        rid=turn_request_id,
        channel_id=message.channel.id,
        user_id=message.author.id,
        task=asyncio.current_task(),
    )
    try:
        _runtime_resources.register_job(
            job,
            register=register_job,
            unregister=unregister_job,
        )
    except RuntimeError:
        # close() can start after the optimistic accepting_jobs check above.
        try:
            await message.channel.send(
                "サービスを再起動しています。起動完了後に再送してください。"
            )
        except Exception:
            pass
        return

    # Reserve FIFO arrival order synchronously before status/reaction I/O.
    # The outer guard starts immediately after registration so even ticket or
    # StatusController construction failure cannot leak the active JobState.
    ticket: AdmissionTicket | None = None
    status: bot_ui.StatusController | None = None
    sema_acquired = False
    try:
        ticket = sema.issue()
        was_queued = ticket.position > 1
        # Status/spinner UI — one writer for the status message (R2, bot_ui.py).
        status = bot_ui.StatusController(message, track_task=_track_task)
        if was_queued:
            wait_pos = max(1, ticket.position - 1)
            await status.initial_reply(was_queued=True, wait_pos=wait_pos)
            log.info(
                "Message %s queued in channel %s (pos=%d)",
                message.id,
                message.channel.id,
                wait_pos,
            )
        else:
            await status.initial_reply(was_queued=False)

        # 👀/⏳ reaction marker on the user's message (restored 2026-06-13,
        # haroin request). The job-state status reply above replaced the old
        # reaction-only flow, but the reaction is the at-a-glance "seen" signal
        # on the message itself — keep BOTH: ⏳ while queued, 👀 once processing.
        if was_queued:
            try:
                await message.add_reaction("⏳")  # ⏳ queued
            except discord.errors.HTTPException:
                pass
        else:
            try:
                await message.add_reaction("\U0001f440")  # 👀 processing
            except discord.errors.HTTPException:
                pass
        await ticket.wait()
        sema_acquired = True
    except asyncio.CancelledError as _cancel:
        # Cancellation while still queued — the semaphore is NOT held here, so
        # there is nothing to release.
        job.phase = JobPhase.FAILED
        if status is not None:
            status.cancel_pending()
        _reason, _cancel_kind = _classify_cancellation(_cancel)
        if _cancel_kind == "shutdown":
            _final_note = (
                "サービス再起動の待機上限に達したため処理を中断しました。"
                "再送してください。"
            )
        elif _cancel_kind == "user_abort":
            _final_note = "⏹️ 待機中に中断されました"
        else:
            _final_note = "待機処理が中断されました。再送してください。"
        try:
            if status is not None:
                await status.edit_final(_final_note)
        finally:
            log.info(
                "Message %s cancelled while queued in channel %s (reason=%s)",
                message.id,
                message.channel.id,
                _cancel_kind,
            )
        return
    except BaseException:
        job.phase = JobPhase.FAILED
        if status is not None:
            status.cancel_pending()
        raise
    finally:
        if not sema_acquired:
            if ticket is not None:
                ticket.cancel()
            _runtime_resources.unregister_job(job)
            if status is not None:
                await status.close()
            for marker in ("⏳", "\U0001f440"):
                try:
                    await message.remove_reaction(marker, bot.user)
                except (discord.errors.HTTPException, AttributeError):
                    pass

    assert status is not None
    assert ticket is not None

    mem0_user_id = str(message.author.id)
    image_paths: list[str] = []
    binary_paths: list[str] = []
    is_thread = isinstance(message.channel, discord.Thread)
    context_result = None
    _renderer = None
    delivery = bot_publish.DeliveryState(
        request_id=turn_request_id,
        receipt_callback=lambda state: _turn_outbox.run_io(
            _turn_outbox.record_delivery,
            turn_request_id,
            message_ids=state.message_ids,
            streamed=state.streamed,
        ),
    )

    async def _record_stream_delivery(sent_message) -> None:
        delivery.streamed = True
        await delivery.record_message(
            sent_message,
            on_first_ack=status.delete_if_alive,
        )

    async def _register_turn_archive(**kwargs) -> None:
        await _turn_outbox.run_io(
            _turn_outbox.add_archive_job,
            turn_request_id,
            **kwargs,
        )

    def _register_turn_renderer(renderer) -> None:
        nonlocal _renderer
        _renderer = renderer
        _runtime_resources.register_renderer(renderer)

    try:
        # Stage the immutable request and its archive intent only after FIFO
        # admission. No model/stream callback can acknowledge a turn whose
        # durable request payload has not been atomically written.
        await _turn_outbox.run_io(
            _turn_outbox.stage_request,
            turn_request_id,
            request_message_id=message.id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            static_payload={
                "caller_is_admin": caller_is_admin,
                "raw_user_text": raw_user_text,
                "response_text": "",
                "mem0_user_id": mem0_user_id,
                "conv_context": "",
                "channel_name": getattr(message.channel, "name", "DM"),
                "session_info": None,
            },
            archive_intent={
                "kind": "generated_response_images",
                "channel_id": message.channel.id,
                "request_message_id": message.id,
                "direction": "outbound",
            },
        )
        job.phase = JobPhase.PREPROCESSING
        if was_queued:
            await status.update("⏳ 考え中...")
            # Swap ⏳→👀 now that processing actually starts.
            try:
                await message.remove_reaction("⏳", bot.user)
                await message.add_reaction("\U0001f440")
            except discord.errors.HTTPException:
                pass
            log.info(
                "Dequeued message %s in channel %s, processing now",
                message.id,
                message.channel.id,
            )

        _provider_will_resume = provider_resume_hint(
            _provider,
            message.channel.id,
            message.author.id,
        )
        context_result = await bot_context.prepare_context(
            bot=bot,
            message=message,
            status=status,
            prompt=prompt,
            raw_user_text=raw_user_text,
            caller_is_admin=caller_is_admin,
            mem0_user_id=mem0_user_id,
            will_resume_override=_provider_will_resume,
            image_paths=image_paths,
            binary_paths=binary_paths,
            config=bot_context.ContextConfig(
                admin_user_ids=ADMIN_USER_IDS,
                max_user_message_chars=MAX_USER_MESSAGE_CHARS,
                mem0_min_query_length=MEM0_MIN_QUERY_LENGTH,
                mem0_smart_search=MEM0_SMART_SEARCH,
                mem0_recall_limit=MEM0_RECALL_LIMIT,
                mem0_recall_limit_restricted=MEM0_RECALL_LIMIT_RESTRICTED,
                mem0_history_search_enabled=MEM0_HISTORY_SEARCH_ENABLED,
                mem0_history_search_limit=MEM0_HISTORY_SEARCH_LIMIT,
                mem0_history_embed_enabled=MEM0_HISTORY_EMBED_ENABLED,
                mem0_history_embed_limit=MEM0_HISTORY_EMBED_LIMIT,
                attachment_limits=bot_admission.AttachmentLimits(
                    image_content_types=IMAGE_CONTENT_TYPES,
                    text_content_types=TEXT_CONTENT_TYPES,
                    max_image_bytes=MAX_ATTACHMENT_BYTES,
                    max_text_bytes=MAX_TEXT_ATTACHMENT_BYTES,
                    max_arbitrary_bytes=MAX_ARBITRARY_ATTACHMENT_BYTES,
                ),
            ),
            deps=bot_context.ContextDeps(
                launch_context_tasks=bot_preprocess.launch_context_tasks,
                sanitize=_sanitize_untrusted_text,
                cap_text=_cap_text,
                get_session=get_session,
                is_session_fresh=_is_session_fresh,
                fetch_thread_context=fetch_thread_context,
                fetch_channel_context=fetch_channel_context,
                resolve_message_links=resolve_message_links,
                resolve_channel_mention_contexts=resolve_channel_mention_contexts,
                search_memories_smart=search_memories_smart,
                should_use_memory_llm_mode=should_use_memory_llm_mode,
                get_http_session=get_http_session,
                download_attachments=download_attachments,
                attachment_metadata_by_path=attachment_metadata_by_path,
                archive_images=r2_archive.archive_images,
                download_text_attachments=download_text_attachments,
                download_arbitrary_attachments=download_arbitrary_attachments,
                classify_attachment_rejections=_classify_attachment_rejections,
                create_task=_runtime_resources.create,
            ),
        )
        will_resume = context_result.will_resume
        conv_context = context_result.conv_context
        text_attachments = context_result.text_attachments
        arbitrary_attachments = context_result.arbitrary_attachments
        _attach_rejected = context_result.attachment_rejections

        prompt_result = await bot_prompt.build_prompt(
            bot_prompt.PromptInput(
                message=message,
                job=job,
                status=status,
                request_key=_request_key,
                caller_is_admin=caller_is_admin,
                mem0_user_id=mem0_user_id,
                will_resume=will_resume,
                reply_context=context_result.reply_context,
                reply_is_trusted=context_result.reply_is_trusted,
                resolved_prompt=context_result.resolved_prompt,
                conv_context=context_result.conv_context,
                cross_thread_context=context_result.cross_thread_context,
                forwarded_context=context_result.forwarded_context,
                image_paths=image_paths,
                text_attachments=text_attachments,
                arbitrary_attachments=arbitrary_attachments,
                mem0_task=context_result.mem0_task,
                invest_kb_task=context_result.invest_kb_task,
                history_search_task=context_result.history_search_task,
                history_embed_task=context_result.history_embed_task,
                skill_overrides=_SKILL_OVERRIDES,
                attachment_artifacts=context_result.attachment_artifacts,
            ),
            config=bot_prompt.PromptConfig(
                max_prompt_chars=MAX_PROMPT_CHARS,
                sleep_enforce_user_id=SLEEP_ENFORCE_USER_ID,
                sleep_enforce_enabled=SLEEP_ENFORCE_ENABLED,
                sleep_enforce_start_hour=SLEEP_ENFORCE_START_HOUR,
                sleep_enforce_end_hour=SLEEP_ENFORCE_END_HOUR,
            ),
            deps=bot_prompt.PromptDeps(
                sanitize=_sanitize_untrusted_text,
                wrap_user_input=_wrap_user_input,
                cap_text=_cap_text,
                load_history_channel=load_history_channel,
                format_memories_for_prompt=format_memories_for_prompt,
                format_history_for_prompt=format_history_for_prompt,
                render_active_skill_prompt=render_active_skill_prompt,
                skill_error_type=SkillError,
                get_request_id=get_request_id,
                prompt_builder=PromptBuilder,
            ),
            recalling_phase=JobPhase.RECALLING,
        )
        prompt = prompt_result.prompt
        shared_context = await guild_context.context_for(
            message, bot, max_chars=min(24000, max(0, MAX_PROMPT_CHARS - len(prompt) - 2)),
        )
        if shared_context:
            # Also inject on native thread resume, which otherwise skips the
            # other speakers' messages in the full channel context.
            prompt = f"{shared_context}\n\n{prompt}"
        _active_skill_prompt = prompt_result.active_skill_prompt

        model_result = await bot_model.invoke_model(
            bot_model.ModelInput(
                bot=bot,
                message=message,
                status=status,
                job=job,
                prompt=prompt,
                sessions=sessions,
                provider=_provider,
                caller_is_admin=caller_is_admin,
                is_thread=is_thread,
                raw_user_text=raw_user_text,
                image_paths=image_paths,
                text_attachments=text_attachments,
                arbitrary_attachments=arbitrary_attachments,
                forced_model=_forced_model,
                forced_effort=_forced_effort,
                active_skill_prompt=_active_skill_prompt,
                tier_overrides=_TIER_OVERRIDES,
            ),
            config=bot_model.ModelConfig(
                scratchpad_stream_enabled=SCRATCHPAD_STREAM_ENABLED,
                plan_stream_enabled=PLAN_STREAM_ENABLED,
                math_presentation_mode=MATH_PRESENTATION_MODE,
                deliberation_router_enabled=DELIB_ROUTER_ENABLED,
                spinner_mode=SPINNER_MODE,
                admin_user_ids=ADMIN_USER_IDS,
            ),
            deps=bot_model.ModelDeps(
                live_renderer=bot_ui.LiveRenderer,
                track_task=_track_task,
                split_message=split_message,
                speaker_label=speaker_label,
                provider_label=provider_label,
                call_with_tools=call_with_tools,
                register_renderer=_register_turn_renderer,
                delivery_callback=_record_stream_delivery,
            ),
            calling_phase=JobPhase.CALLING_CLAUDE,
        )
        response_text = model_result.response_text
        session_info = model_result.session_info
        actually_resumed = model_result.actually_resumed
        _renderer = model_result.renderer

        job.phase = JobPhase.FINALIZING
        await status.update("🪄 整形中")

        if _provider == PROVIDER_CLAUDE and will_resume and not actually_resumed:
            # actually_resumed is the 3rd return value from call_with_tools.
            # It can be False for several reasons: a genuine MCP-set mismatch,
            # a context_overflow that intentionally drops the session, OR a
            # transient-retry exhaustion that returned None (the entry.py path
            # for overloaded/rate_limit). Do NOT assert a specific cause here —
            # the old "likely MCP mismatch" wording misdiagnosed the ADR-0006
            # overloaded bug. Just record the observable fact.
            log.warning(
                "[rid=%s] will_resume/resumable mismatch: bot expected resume "
                "but entry.py returned not-resumed (MCP change, context "
                "overflow, or transient-retry exhaustion). Lean prompt was "
                "sent without JSONL context.",
                get_request_id(),
            )

        prepared_response = await bot_publish.prepare_response(
            message=message,
            status=status,
            renderer=_renderer,
            response_text=response_text,
            raw_user_text=raw_user_text,
            strip_numeric_inline_code=strip_numeric_inline_code,
        )
        delivery.streamed = bool(delivery.streamed or _renderer.live_sent_any)
        delivery.body_delivered = bool(
            delivery.body_delivered or _renderer.live_sent_any
        )
        if prepared_response is None and not delivery.body_delivered:
            return
        response_text = (
            prepared_response
            if prepared_response is not None
            else (getattr(_renderer, "committed", "") or "")
        )
        record_response_text = response_text or (
            getattr(_renderer, "committed", "") or ""
        )
        await _turn_outbox.run_io(
            _turn_outbox.prepare,
            turn_request_id,
            {
                "caller_is_admin": caller_is_admin,
                "raw_user_text": raw_user_text,
                "response_text": record_response_text,
                "mem0_user_id": mem0_user_id,
                "conv_context": str(conv_context or ""),
                "channel_name": getattr(message.channel, "name", "DM"),
                "session_info": _serialize_session_info(session_info),
            },
        )
        turn_committed = False

        async def _commit_delivered_turn() -> None:
            nonlocal turn_committed
            if turn_committed:
                return
            await _turn_outbox.run_io(
                _turn_outbox.record_delivery,
                turn_request_id,
                message_ids=delivery.message_ids,
                streamed=delivery.streamed,
            )
            await _reconcile_turn_outbox(turn_request_id)
            turn_committed = True

        try:
            published = await bot_publish.publish_response(
                message=message,
                bot_user=bot.user,
                renderer=_renderer,
                response_text=response_text,
                caller_is_admin=caller_is_admin,
                attachment_rejections=_attach_rejected,
                deps=bot_publish.PublishDeps(
                    normalize_image_filenames=normalize_image_filenames,
                    extract_image_paths=extract_image_paths,
                    extract_file_paths=extract_file_paths,
                    is_attach_ignored=is_attach_ignored,
                    archive_images=r2_archive.archive_images,
                    bind_response_message=r2_archive.bind_response_message,
                    parse_segments=parse_segments,
                    mask_code_blocks=mask_code_blocks,
                    has_tables=has_tables,
                    has_math=has_math,
                    split_message=split_message,
                    reply_chain_sender=ReplyChainSender,
                    send_segmented=_send_segmented,
                    log_math_presentation=_log_math_presentation,
                ),
                delivery_state=delivery,
                on_first_ack=status.delete_if_alive,
                register_archive=_register_turn_archive,
            )
            if isinstance(published, bot_publish.DeliveryState):
                delivery = published
        except BaseException:
            if delivery.body_delivered:
                await _commit_delivered_turn()
            raise
        if delivery.body_delivered:
            await _commit_delivered_turn()
    except asyncio.CancelledError as _cancel:
        job.phase = JobPhase.FAILED
        # ADR-0038: distinguish a real /abort (cancel("user abort")) from a
        # StallWatchdog backstop firing (cancel("stream idle")) and a forced
        # shutdown after the graceful drain deadline. tool_loop's stream guard normally recovers a
        # stall before this, so reaching here on a stall means an outer wedge —
        # label it honestly instead of blaming a "/abort" that never happened.
        _reason, _cancel_kind = _classify_cancellation(_cancel)
        if _cancel_kind == "stream_idle":
            log.warning(
                "[rid=%s] on_message cancelled: stream idle backstop "
                "(channel=%s, user=%s)",
                get_request_id(),
                message.channel.id,
                message.author.id,
            )
            _final_note = "⏰ 応答待ちが続いたため中断しました"
        elif _cancel_kind == "shutdown":
            log.warning(
                "[rid=%s] on_message cancelled after shutdown drain timeout "
                "(channel=%s, user=%s)",
                get_request_id(),
                message.channel.id,
                message.author.id,
            )
            _final_note = (
                "サービス再起動の待機上限に達したため処理を中断しました。"
                "再送してください。"
            )
        elif _cancel_kind == "user_abort":
            log.info(
                "[rid=%s] on_message cancelled via /abort (channel=%s, user=%s)",
                get_request_id(),
                message.channel.id,
                message.author.id,
            )
            _final_note = "⏹️ 中断しました"
        else:
            log.warning(
                "[rid=%s] on_message cancelled for unexpected reason=%r "
                "(channel=%s, user=%s)",
                get_request_id(),
                _reason,
                message.channel.id,
                message.author.id,
            )
            _final_note = "処理が中断されました。再送してください。"
        # Leave a short note on the status message instead of deleting it
        # (P0: errors keep a brief explanation; success deletes).
        try:
            if status.status_msg is not None:
                await status.edit_final(_final_note)
            else:
                await message.channel.send(_final_note)
        except Exception:
            pass
    except OAuthRefreshFailed:
        # The OAuth refresh token expired (invalid_grant) — every Anthropic
        # call fails until a human re-authenticates. Tell the caller exactly
        # what to do instead of the generic error. After `/login` rewrites the
        # credentials, ensure_valid_token() reloads them, so the next message
        # recovers with no restart. Log at warning (not exception) — the cause
        # is known, the stack trace adds noise.
        job.phase = JobPhase.FAILED
        log.warning(
            "OAuth refresh token expired; prompting for /login (user ...%s in #%s)",
            str(message.author.id)[-4:],
            message.channel,
        )
        oauth_text = (
            "⚠️ OAuth トークンが失効しています。"
            "ターミナルで `/login` を実行して再認証してください。"
            "（再認証後は次のメッセージから自動で復帰します）"
        )
        try:
            if status.status_msg is not None:
                await status.edit_final(oauth_text)
            else:
                await message.reply(oauth_text)
        except Exception:
            pass
    except CodexAuthError:
        job.phase = JobPhase.FAILED
        log.warning(
            "Codex ChatGPT OAuth unavailable (user ...%s in #%s)",
            str(message.author.id)[-4:],
            message.channel,
        )
        auth_text = (
            "CodexのChatGPT認証が利用できません。サーバー上で "
            "`/home/ubuntu/.volta/bin/codex login` を実行してください。"
        )
        try:
            if status.status_msg is not None:
                await status.edit_final(auth_text)
            else:
                await message.reply(auth_text)
        except Exception:
            pass
    except (CodexUnavailableError, CodexProtocolError, CodexTurnError) as exc:
        job.phase = JobPhase.FAILED
        log.warning(
            "Codex provider failed for user ...%s in #%s: %s",
            str(message.author.id)[-4:],
            message.channel,
            exc,
            exc_info=True,
        )
        err_text = "Codexの実行に失敗しました。サーバーログを確認してください。"
        rid = get_request_id()
        if caller_is_admin and rid:
            err_text += f"\n-# req: {rid}"
        try:
            if status.status_msg is not None:
                await status.edit_final(err_text)
            else:
                await message.reply(err_text)
        except Exception:
            pass
    except Exception:
        job.phase = JobPhase.FAILED
        log.exception(
            "Error processing message from user ...%s in #%s",
            str(message.author.id)[-4:],
            message.channel,
        )
        err_text = "❌ エラーが発生しました。再試行してください。"
        rid = get_request_id()
        if caller_is_admin and rid:
            # Short request id so the admin can grep the logs (P2).
            err_text += f"\n-# req: {rid}"
        try:
            if status.status_msg is not None:
                await status.edit_final(err_text)
            else:
                await message.reply(err_text)
        except Exception:
            pass
    finally:
        if job.phase not in (JobPhase.FAILED,):
            job.phase = JobPhase.DONE
        _runtime_resources.unregister_job(job)
        ticket.release()
        try:
            await status.close()
            for marker in ("⏳", "\U0001f440"):
                try:
                    await message.remove_reaction(marker, bot.user)
                except (discord.errors.HTTPException, AttributeError):
                    pass
        finally:
            try:
                if context_result is not None:
                    await context_result.close()
            finally:
                try:
                    await _runtime_resources.release_renderer(_renderer)
                finally:
                    await asyncio.to_thread(
                        cleanup_attachments,
                        [*image_paths, *binary_paths],
                    )
                    turn_snapshot = await _turn_outbox.run_io(
                        _turn_outbox.snapshot, turn_request_id
                    )
                    if turn_snapshot is not None and turn_snapshot.get(
                        "delivery", {}
                    ).get("acknowledged"):
                        await _reconcile_turn_outbox(turn_request_id)
                    else:
                        await _turn_outbox.run_io(
                            _turn_outbox.discard_unacknowledged, turn_request_id
                        )

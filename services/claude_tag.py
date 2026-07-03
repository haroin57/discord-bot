"""claude tag — :claude: リアクションでタスクをスレッド処理する (ADR-0027).

admin が任意メッセージに :claude:（サーバー固有のカスタム絵文字）でリアクションを
付けると、そのメッセージ（=割り振られたタスク）を整理してプロンプト化し、対象メッセージ
にスレッドを立てて Claude に処理させる。Slack の "Claude Tag"（2026-06-23 発表）の
reaction-trigger 部分を Discord に移植したもの。

設計の要点:
- スレッドは独立した channel_id を持つので、タスクセッションは親チャンネルの雑談
  セッションと自動的に分離される（on_message との session/JSONL 競合が構造的に回避
  される）。後続のスレッド内チャットは既存 on_message が同一 thread.id で継続する。
- 二層権限: 「発火権限」= リアクションした admin（passes_cheap_gate）。「タスク本文の
  信頼レベル」= メッセージ著者の admin 判定（wrap_user_input で trusted/untrusted を
  分ける）。admin が非admin のメッセージを :claude: した場合、本文は untrusted データ
  として隔離されるが、処理自体は admin 権限（restricted=False, 全ツール）で走る。
- 冪等性は三段: in-flight set（プロセス内同時二重発火）/ msg.thread の存在（再起動を
  跨ぐ・Discord 永続）/ bot の ✅ リアクション（スレッドを立てない経路の補助）。

bot.py への循環 import を避けるため、bot-module が所有する可変状態（sessions 辞書・
channel semaphore レジストリ・http session シングルトン・background task tracker・
_send_segmented）は TagDeps 経由で注入する。それ以外（call_with_tools / prompt
builder / 添付 DL / 文脈取得 / job_state / 絵文字）はこのモジュールが直接 import する。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

import discord

from config import (
    ADMIN_USER_IDS,
    MAX_PROMPT_CHARS,
    speaker_label,
)
from services.claude_api import call_with_tools
from services.prompt_builder import PromptBuilder
from services.sentinel import sanitize_untrusted_text
from services.job_state import (
    JobPhase,
    JobState,
    register_job,
    unregister_job,
)
from services.claude_api.progress import SPINNER_EMOJI
from utils.attachments import (
    cleanup_attachments,
    download_arbitrary_attachments,
    download_attachments,
    download_text_attachments,
)
from utils.discord_helpers import fetch_channel_context, fetch_thread_context

try:  # correlation is optional (bot.py also imports it defensively)
    from services.correlation import new_request_id
except ImportError:  # pragma: no cover - defensive fallback
    def new_request_id() -> str:  # type: ignore[misc]
        return ""

log = logging.getLogger(__name__)

# Reaction markers on the *source* message (the task). 👀 = picked up,
# ✅ = done, ❌ = failed. ✅ doubles as a cross-restart idempotency signal for
# the (rare) thread-less fallback path; the durable signal for the normal path
# is the message having a thread.
_SEEN = "\U0001f440"   # 👀
_DONE = "✅"       # ✅
_FAIL = "❌"       # ❌

# Process-local guard against the same message being processed twice
# concurrently (Discord can redeliver reaction events; a user can remove+re-add
# the reaction). Cross-restart idempotency is handled by _already_processed.
_inflight: set[int] = set()

# Discord thread name hard cap is 100 chars; stay under it.
_THREAD_NAME_MAX = 90
_THREAD_AUTO_ARCHIVE_MIN = 1440  # 24h


@dataclass
class TagDeps:
    """bot-module-owned dependencies injected into the orchestrator.

    Everything here lives in bot.py as module-level state/functions and cannot
    be imported from this module without a cycle (bot.py imports claude_tag).
    """

    bot: discord.Client
    sessions: dict
    send_segmented: Callable      # bot._send_segmented(message, text, files)
    get_channel_sema: Callable    # bot._get_channel_sema(channel_id) -> Semaphore
    get_http_session: Callable    # bot.get_http_session() -> aiohttp.ClientSession
    track_task: Callable          # bot._track_task(coro, *, name=None) -> Task


def passes_cheap_gate(
    payload: discord.RawReactionActionEvent,
    *,
    bot_user_id: int,
    admin_ids: set[int],
    emoji_id: int | None,
    emoji_name: str,
) -> bool:
    """Cheap, side-effect-free gate run on EVERY reaction add.

    Returns True only when the heavy task-processing path should run:
    a custom :claude: emoji, added by an admin (not the bot itself), inside a
    guild. Pure so it is trivially unit-testable.
    """
    if payload.user_id == bot_user_id:
        # The bot adds 👀/✅/❌ markers itself — never recurse on those.
        return False
    if payload.guild_id is None:
        # Guild custom emoji can't be used in DMs; the task flow needs a guild
        # channel to anchor a thread on. DMs are out of scope.
        return False
    if payload.user_id not in admin_ids:
        return False
    emoji = payload.emoji
    if emoji is None or emoji.id is None:
        # emoji.id is None for unicode emoji; :claude: is a custom emoji.
        return False
    if emoji_id is not None:
        return emoji.id == emoji_id
    # No ID pinned — fall back to name match (less safe across guilds).
    return emoji.name == emoji_name


async def handle_claude_tag(
    payload: discord.RawReactionActionEvent, deps: TagDeps
) -> None:
    """Orchestrate one :claude: task. The only entry point from bot.py.

    Holds all failures internally — a raised exception here would surface as an
    unhandled error in the on_raw_reaction_add event task, so we swallow
    everything except CancelledError (which must propagate so /abort works).
    """
    msg_id = payload.message_id
    if msg_id in _inflight:
        return
    _inflight.add(msg_id)
    bot_user_id = deps.bot.user.id if deps.bot.user else 0
    msg: discord.Message | None = None
    try:
        msg = await _fetch_target(payload, deps.bot)
        if msg is None:
            return
        if _already_processed(msg, bot_user_id):
            log.info("claude_tag: message %s already processed; skipping", msg_id)
            return

        await _safe_add_reaction(msg, _SEEN)
        send_target, anchor = await _ensure_thread(msg, deps)
        if anchor is None:
            log.warning("claude_tag: could not post anchor for message %s", msg_id)
            return

        sema = deps.get_channel_sema(send_target.id)
        job = JobState(
            rid=new_request_id(),
            channel_id=send_target.id,
            user_id=payload.user_id,
            task=asyncio.current_task(),
        )
        register_job(job)

        acquired = False
        ok = False
        image_paths: list[str] = []
        try:
            try:
                await sema.acquire()
                acquired = True
            except asyncio.CancelledError:
                # /abort while still queued — semaphore not held, nothing to
                # release here (the outer finally still runs).
                await _safe_edit(anchor, "⏹️ 待機中に中断されました")
                raise
            job.phase = JobPhase.PREPROCESSING
            prompt, image_paths, has_image, raw_text = await _build_task_prompt(
                msg, payload, deps
            )
            job.phase = JobPhase.CALLING_CLAUDE
            ok = await _process(
                send_target, anchor, prompt, image_paths, has_image,
                raw_text, payload.user_id, deps,
            )
        finally:
            job.phase = JobPhase.DONE if ok else JobPhase.FAILED
            unregister_job(job)
            if acquired:
                sema.release()
            if image_paths:
                await asyncio.to_thread(cleanup_attachments, image_paths)

        await _safe_add_reaction(msg, _DONE if ok else _FAIL)
        await _safe_remove_reaction(msg, _SEEN, deps.bot.user)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("claude_tag: unhandled error for message %s", msg_id)
        if msg is not None:
            await _safe_add_reaction(msg, _FAIL)
    finally:
        _inflight.discard(msg_id)


async def _fetch_target(
    payload: discord.RawReactionActionEvent, bot: discord.Client
) -> discord.Message | None:
    """Resolve the reacted-to message from a raw payload.

    Raw reaction events carry no message body, so we fetch it. Deleted /
    forbidden / transient failures resolve to None — the handler silently
    gives up (a reaction handler must never crash the gateway).
    """
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    fetch = getattr(channel, "fetch_message", None)
    if fetch is None:
        return None
    try:
        return await fetch(payload.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def _already_processed(msg: discord.Message, bot_user_id: int) -> bool:
    """True if this message was already turned into a task.

    Two durable signals (survive a bot restart, unlike the in-memory
    ``_inflight`` set): the message already has a thread started from it, or the
    bot already reacted ✅ (the fallback path that couldn't make a thread).
    """
    if getattr(msg, "thread", None) is not None:
        return True
    for reaction in getattr(msg, "reactions", None) or []:
        if getattr(reaction, "me", False) and str(reaction.emoji) == _DONE:
            return True
    return False


async def _ensure_thread(
    msg: discord.Message, deps: TagDeps
) -> tuple[discord.abc.Messageable, discord.Message | None]:
    """Return ``(send_target, anchor)`` for processing.

    Normal path: create a public thread anchored on the task message and post a
    status anchor inside it — its independent channel_id isolates the task
    session. If the message already lives in a thread, reuse that thread. If a
    thread can't be created (permissions / channel type), degrade to replying in
    the parent channel so the feature still works.

    ``anchor`` is a bot-owned message the status callback edits and that
    ``_send_segmented`` replies to (so the response lands in ``send_target``).
    """
    channel = msg.channel
    thread: discord.Thread | None = None
    if isinstance(channel, discord.Thread):
        thread = channel
    else:
        try:
            thread = await msg.create_thread(
                name=_thread_name(msg),
                auto_archive_duration=_THREAD_AUTO_ARCHIVE_MIN,
            )
        except (discord.HTTPException, discord.Forbidden, AttributeError) as exc:
            log.warning(
                "claude_tag: create_thread failed for %s (%s); "
                "falling back to parent-channel reply",
                msg.id, exc,
            )
            thread = None

    status = _status_text("タスクを整理しています…")
    if thread is not None:
        try:
            anchor = await thread.send(status)
        except discord.HTTPException:
            return thread, None
        return thread, anchor

    # Fallback: reply in the parent channel. send_target = parent channel; the
    # channel semaphore is still shared with on_message so no corruption, but
    # the task shares the channel session (acceptable in this degraded path).
    try:
        anchor = await msg.reply(status)
    except discord.HTTPException:
        return msg.channel, None
    return msg.channel, anchor


def _thread_name(msg: discord.Message) -> str:
    """Derive a thread name from the task message (<=90 chars, never empty)."""
    base = (msg.content or "").strip().replace("\n", " ")
    if not base:
        # Image/file-only task or empty body.
        base = "claude task"
    return base[:_THREAD_NAME_MAX] or "claude task"


def _status_text(body: str) -> str:
    return f"{SPINNER_EMOJI} {body}" if SPINNER_EMOJI else body


async def _build_task_prompt(
    msg: discord.Message,
    payload: discord.RawReactionActionEvent,
    deps: TagDeps,
) -> tuple[str, list[str], bool, str]:
    """Assemble the "整理" prompt. Returns (prompt, image_paths, has_image, raw_text).

    Trust model:
      - reactor (admin) issues a *trusted* instruction to process the task.
      - the task body (the source message) is wrapped by the AUTHOR's trust
        level — a non-admin author's content lands in <untrusted_user_input>.
    """
    author = msg.author
    author_is_admin = getattr(author, "id", None) in ADMIN_USER_IDS
    raw_content = msg.content or ""

    # --- attachments on the task message (mirror on_message) ---
    http = await deps.get_http_session()
    all_atts = list(getattr(msg, "attachments", None) or [])
    image_paths = await download_attachments(http, all_atts)
    text_atts = await download_text_attachments(http, all_atts)
    arbitrary_atts = await download_arbitrary_attachments(
        http, all_atts, caller_is_admin=author_is_admin,
    )

    # --- parent channel / thread context (Q2: message + channel context) ---
    conv_context = ""
    try:
        channel = msg.channel
        if isinstance(channel, discord.Thread):
            conv_context = await fetch_thread_context(
                channel, msg, caller_is_admin=True,
            )
        else:
            conv_context = await fetch_channel_context(
                channel, msg, caller_is_admin=True,
            )
    except Exception:
        log.warning("claude_tag: context fetch failed (non-fatal)", exc_info=True)

    # --- <task_source> block: the message body + its attachments ---
    author_name = sanitize_untrusted_text(getattr(author, "display_name", "") or "unknown")
    jump_url = getattr(msg, "jump_url", "") or ""
    src: list[str] = [
        f'<task_source author="{author_name}" link="{jump_url}">',
        sanitize_untrusted_text(raw_content),
    ]
    if image_paths:
        instructions = "\n".join(
            f"- {p} (Use the Read tool to view this image)" for p in image_paths
        )
        src.append(f"<attached_images>\n{instructions}\n</attached_images>")
    for fname, content in text_atts:
        safe_fname = sanitize_untrusted_text(fname)
        safe_content = sanitize_untrusted_text(content)
        src.append(f'<attached_file name="{safe_fname}">\n{safe_content}\n</attached_file>')
    if arbitrary_atts:
        af = ['<attached_files note="binary uploads saved to disk; use Read/Bash to inspect">']
        for meta in arbitrary_atts:
            safe_fname = sanitize_untrusted_text(meta["filename"])
            safe_mime = sanitize_untrusted_text(meta["mime"])
            af.append(
                f"- path={meta['path']} filename={safe_fname!r} "
                f"mime={safe_mime!r} size={meta['size']}"
            )
        af.append("</attached_files>")
        src.append("\n".join(af))
    src.append("</task_source>")
    task_body = PromptBuilder.wrap_user_input(
        "\n".join(src), is_admin=author_is_admin,
    )

    # --- reactor's trusted framing instruction ---
    reactor = payload.member
    reactor_name = sanitize_untrusted_text(
        getattr(reactor, "display_name", "") or "admin"
    )
    reactor_tag = sanitize_untrusted_text(str(reactor) if reactor else "admin")
    header = PromptBuilder.build_header(reactor_name, reactor_tag, True)
    instruction = PromptBuilder.wrap_trusted(
        ":claude: リアクションで処理を依頼されたタスクです。下の <task_source> の内容を"
        "整理し、必要なら段階に分解して実行してください。完了したらこのスレッドに結果を"
        "報告してください。"
    )
    time_line = PromptBuilder.build_time_line()

    parts = [header, instruction, time_line]
    if conv_context:
        parts.append(
            f"<channel_context>\n{sanitize_untrusted_text(conv_context)}\n</channel_context>"
        )
    parts.append(task_body)
    prompt = PromptBuilder.cap_text("\n".join(parts), MAX_PROMPT_CHARS)
    return prompt, image_paths, bool(image_paths), raw_content


async def _process(
    send_target: discord.abc.Messageable,
    anchor: discord.Message,
    prompt: str,
    image_paths: list[str],
    has_image: bool,
    raw_text: str,
    reactor_id: int,
    deps: TagDeps,
) -> bool:
    """Run the prompt through call_with_tools and post the result to the thread.

    Reuses the bot's full processing pipeline (MCP selection, session resume,
    tool loop, retries, JSONL persistence) keyed by ``send_target.id`` — for a
    thread that is the thread's own id, isolating the task session.
    """
    is_thread = isinstance(send_target, discord.Thread)
    last_edit = {"t": 0.0}

    async def status_callback(text: str) -> None:
        # Throttled, latest-dropped status edits. Fire-and-forget so the agent
        # loop is never blocked on Discord I/O (Discord sustains ~1 edit/s).
        now = time.monotonic()
        if now - last_edit["t"] < 1.5:
            return
        last_edit["t"] = now
        body = _status_text(text)
        deps.track_task(_safe_edit(anchor, body[:1990]), name="claude_tag_status")

    response_text, _session, _resumed = await call_with_tools(
        prompt,
        send_target.id,
        deps.sessions,
        status_callback=status_callback,
        restricted=False,                 # reactor=admin guaranteed by the gate
        raw_user_text=raw_text,
        is_thread=is_thread,
        has_image_attachment=has_image,
        author=speaker_label(reactor_id),
        user_id=reactor_id,
    )

    response_text = (response_text or "").strip()
    if not response_text:
        await _safe_edit(anchor, "❌ 応答が空でした。もう一度試してください。")
        return False

    await deps.send_segmented(anchor, response_text, [])
    await _safe_edit(anchor, "-# ✅ タスク処理完了")
    return True


# ---------------------------------------------------------------------------
# Small Discord helpers that swallow the usual transient errors so the
# orchestrator never dies on a cosmetic edit/reaction failure.
# ---------------------------------------------------------------------------
async def _safe_edit(msg: discord.Message, content: str) -> None:
    try:
        await msg.edit(content=content)
    except discord.HTTPException:
        pass


async def _safe_add_reaction(msg: discord.Message, emoji: str) -> None:
    try:
        await msg.add_reaction(emoji)
    except discord.HTTPException:
        pass


async def _safe_remove_reaction(
    msg: discord.Message, emoji: str, member
) -> None:
    if member is None:
        return
    try:
        await msg.remove_reaction(emoji, member)
    except (discord.NotFound, discord.HTTPException):
        pass

"""Discord bot — event handlers and message routing.

Matches free-code QueryEngine pattern: orchestrates services and utils.
"""

import asyncio
import io
import logging
import time
from collections import OrderedDict

import aiohttp
import discord

from config import (
    ADMIN_USER_IDS,
    BOT_KEYWORDS,
    BOT_ROLE_IDS,
    COOLDOWN_SECONDS,
    DISCORD_TOKEN,
    MAX_COOLDOWN_ENTRIES,
    needs_tools,
)
from services.claude_api import call_direct
from services.claude_sdk import call_with_tools
from services.session import load_all_history, load_sessions, update_session
from utils.attachments import (
    cleanup_attachments,
    cleanup_stale_attachments,
    download_attachments,
    extract_image_paths,
)
from utils.discord_helpers import (
    build_mention_regex,
    fetch_thread_context,
    resolve_message_links,
    split_message,
    strip_mention,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
sessions = load_sessions()
conversation_history = load_all_history()
cooldowns: OrderedDict[int, float] = OrderedDict()
channel_busy: set[int] = set()
_http_session: aiohttp.ClientSession | None = None

log.info("Loaded %d sessions, %d conversation histories from disk", len(sessions), len(conversation_history))


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


def is_admin(user: discord.User | discord.Member) -> bool:
    return user.id in ADMIN_USER_IDS



def _purge_cooldowns() -> None:
    now = time.time()
    expired = [uid for uid, ts in cooldowns.items() if now - ts > COOLDOWN_SECONDS * 10]
    for uid in expired:
        del cooldowns[uid]


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ---------------------------------------------------------------------------
# Lifecycle (free-code: graceful shutdown)
# ---------------------------------------------------------------------------
_orig_close = client.close


async def _patched_close() -> None:
    global _http_session
    log.info("Closing bot and cleaning up resources...")
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None
    await _orig_close()


client.close = _patched_close


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@client.event
async def on_ready():
    for guild in client.guilds:
        member = guild.get_member(client.user.id)
        if member:
            for role in member.roles:
                if not role.is_default():
                    BOT_ROLE_IDS.add(role.id)
    build_mention_regex(client.user.id)
    log.info("Bot is ready: %s (id=%s), role_ids=%s", client.user, client.user.id, BOT_ROLE_IDS)


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return

    log.debug("[%s] %s: %s", message.channel, message.author, message.content[:200])

    # In DMs, only admins can interact. In guilds, require mention or keyword.
    is_dm = isinstance(message.channel, discord.DMChannel)
    if is_dm and not is_admin(message.author):
        return
    if not is_dm:
        content_lower = message.content.lower()
        mentioned = (client.user in message.mentions or
                     any(role.id in BOT_ROLE_IDS for role in message.role_mentions) or
                     any(kw in content_lower for kw in BOT_KEYWORDS))
        if not mentioned:
            return

    try:
        await message.add_reaction("👀")
    except discord.errors.Forbidden:
        pass

    # Rate limiting
    _purge_cooldowns()
    now = time.time()
    last = cooldowns.get(message.author.id, 0)
    if now - last < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (now - last))
        await message.reply(f"⏳ Please wait {remaining}s before sending another request.")
        return
    cooldowns[message.author.id] = now
    cooldowns.move_to_end(message.author.id)
    while len(cooldowns) > MAX_COOLDOWN_ENTRIES:
        cooldowns.popitem(last=False)

    raw_user_text = message.content.strip() if is_dm else strip_mention(message.content, client.user.id)
    prompt = raw_user_text
    if not prompt:
        await message.reply("何か用ですか？" if is_dm else "Please include a message after mentioning me!")
        return

    if message.channel.id in channel_busy:
        await message.reply("⏳ 前のリクエストを処理中です。完了まで待ってください。")
        return

    channel_busy.add(message.channel.id)
    image_paths: list[str] = []
    try:
        # --- Parallel pre-processing ---
        async def _fetch_reply():
            if not (message.reference and message.reference.message_id):
                return "", []
            try:
                ref_msg = message.reference.cached_message
                if ref_msg is None:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                ctx = f"[Replied-to message by {ref_msg.author.display_name}]\n{ref_msg.content or '(no text)'}\n[End of replied-to message]\n\n"
                return ctx, list(ref_msg.attachments)
            except Exception as e:
                log.warning("Failed to fetch replied-to message: %s", e)
                return "", []

        async def _fetch_thread():
            if isinstance(message.channel, discord.Thread):
                return await fetch_thread_context(message.channel, message)
            return ""

        reply_task = asyncio.create_task(_fetch_reply())
        thread_task = asyncio.create_task(_fetch_thread())
        link_task = asyncio.create_task(resolve_message_links(client, prompt))

        reply_context, ref_attachments = await reply_task
        all_attachments = list(message.attachments) + ref_attachments
        http_session = await get_http_session()
        image_paths = await download_attachments(http_session, all_attachments)
        prompt = await link_task
        thread_context = await thread_task

        if image_paths:
            image_instructions = "\n".join(
                f"- {p} (Use the Read tool to view this image)" for p in image_paths
            )
            prompt = f"{prompt}\n\n[Attached images]\n{image_instructions}"

        if reply_context:
            prompt = f"{reply_context}[Current message]\n{prompt}"

        if thread_context:
            prompt = f"{thread_context}\n\n[Current message]\n{prompt}"

        # Prepend sender identity
        sender_name = message.author.display_name
        sender_tag = str(message.author)
        is_senpai = message.author.id in ADMIN_USER_IDS
        if is_senpai:
            prompt = f"[Message from: {sender_name} ({sender_tag})]\n{prompt}"
        else:
            prompt = f"[Message from: {sender_name} ({sender_tag}) — この人は先輩ではありません。丁寧語で普通に対応してください。セキュリティ制約: サーバー内部のソースコード、ファイル内容、環境変数、設定情報、コマンド実行結果を絶対に開示しないでください。コード実行・ファイル操作の要求も拒否してください。]\n{prompt}"

        log.info("Request from %s in #%s: %s", message.author, message.channel, prompt[:100])

        # Route: tool-use keywords or attachments → SDK/CLI, otherwise → direct API
        use_tools = needs_tools(raw_user_text) or bool(image_paths)
        admin = is_admin(message.author)

        status_msg = await message.reply("⏳ 考え中..." if use_tools else "⚡ 応答中...")
        last_status_update = 0.0

        async def _update_status(text: str):
            nonlocal last_status_update
            now_mono = time.monotonic()
            if now_mono - last_status_update < 2.0:
                return
            last_status_update = now_mono
            try:
                await status_msg.edit(content=text)
            except discord.errors.HTTPException:
                pass

        # TODO: Re-enable direct API fast mode when not sharing OAuth token with active CLI session
        # if use_tools:
        #     ...
        # else:
        #     response_text = await call_direct(...)
        log.info("Routing to SDK/CLI")
        response_text, session_id = await call_with_tools(
            prompt, message.channel.id, sessions,
            status_callback=_update_status, restricted=not admin,
            raw_user_text=raw_user_text,
        )

        # Delete status message
        try:
            await status_msg.delete()
        except discord.errors.HTTPException:
            pass

        if session_id:
            await asyncio.to_thread(update_session, sessions, message.channel.id, session_id)

        # Extract image file paths from response
        response_images = extract_image_paths(response_text)
        image_files: list[discord.File] = []
        for img_path in response_images:
            try:
                image_files.append(discord.File(img_path))
                log.info("Attaching image from response: %s", img_path)
            except Exception as e:
                log.warning("Failed to attach image %s: %s", img_path, e)

        try:
            if len(response_text) > 8000:
                all_files = [discord.File(io.StringIO(response_text), filename="response.md")] + image_files
                await message.reply("Response is too long, sending as file(s):", files=all_files)
            else:
                chunks = split_message(response_text)
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        if image_files:
                            await message.reply(chunk, files=image_files)
                        else:
                            await message.reply(chunk)
                    else:
                        await message.channel.send(chunk)
        finally:
            for f in image_files:
                f.close()

    except Exception:
        log.exception("Error processing message from %s in #%s", message.author, message.channel)
        await message.reply("❌ エラーが発生しました。")
    finally:
        channel_busy.discard(message.channel.id)
        await asyncio.to_thread(cleanup_attachments, image_paths)

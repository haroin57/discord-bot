"""Prefix (message-body) commands: reply-delete and bulk purge (R1).

Moved verbatim from bot.py's on_message (grand refactor P2-d remainder).
Behavior, permission checks, and user-facing strings are unchanged.
"""

from __future__ import annotations

import asyncio
import logging

import discord

log = logging.getLogger(__name__)

_DELETE_KEYWORDS = {"!delete", "削除", "消して", "delete"}


async def handle_prefix_commands(
    message: "discord.Message",
    bot,
    *,
    bare_text: str,
    is_dm: bool,
    caller_is_admin: bool,
) -> bool:
    """Handle !delete / !purge family. Returns True when the message was a
    prefix command (processed or rejected) and on_message must stop."""

    # --- Quick command: delete replied-to message (admin only) ---
    if bare_text in _DELETE_KEYWORDS and message.reference and message.reference.message_id:
        if not caller_is_admin:
            await message.reply("❌ この操作はadminユーザーのみ使用できます。")
            return True
        try:
            ref_msg = message.reference.cached_message
            if ref_msg is None:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
            await ref_msg.delete()
            # Also delete the command message itself for cleanliness
            try:
                await message.delete()
            except discord.errors.HTTPException:
                pass
            log.info("Deleted message %s (requested by admin %s)", ref_msg.id, message.author.id)
        except discord.errors.NotFound:
            await message.reply("⚠️ そのメッセージはもう存在しません。")
        except discord.errors.Forbidden:
            await message.reply("⚠️ そのメッセージを削除する権限がありません。")
        except Exception as e:
            log.warning("Failed to delete message: %s", e, exc_info=True)
            await message.reply("❌ 削除に失敗しました。ログを確認してください。")
        return True

    # --- Bulk purge of the bot's OWN messages (admin only) ---
    # Three modes, all scoped to messages authored by this bot:
    #   !purge-from   (as a reply) -> delete bot msgs from the replied-to msg onward
    #   !purge-since HH:MM (JST)   -> delete bot msgs since that time today
    #   !purge N                   -> delete the most recent N bot msgs
    # Never deletes other users' messages. Hard-capped at MAX_PURGE.
    _purge_mode = None
    _purge_arg = None
    if bare_text == "!purge-from":
        _purge_mode = "from"
    elif bare_text.startswith("!purge-since "):
        _purge_mode, _purge_arg = "since", bare_text[len("!purge-since "):].strip()
    elif bare_text.startswith("!purge ") and bare_text[len("!purge "):].strip().isdigit():
        _purge_mode, _purge_arg = "count", bare_text[len("!purge "):].strip()

    if not _purge_mode:
        return False

    if not caller_is_admin:
        await message.reply("❌ この操作はadminユーザーのみ使用できます。")
        return True
    from datetime import datetime, timedelta, timezone

    MAX_PURGE = 200
    _me_id = bot.user.id

    def _is_bot_msg(m: "discord.Message") -> bool:
        return m.author.id == _me_id

    try:
        after_dt = None
        count_target = None
        if _purge_mode == "from":
            if not (message.reference and message.reference.message_id):
                await message.reply(
                    "⚠️ `!purge-from` は削除の起点にしたいメッセージに**返信**して送ってください。"
                )
                return True
            ref_msg = message.reference.cached_message
            if ref_msg is None:
                ref_msg = await message.channel.fetch_message(
                    message.reference.message_id
                )
            after_dt = ref_msg.created_at - timedelta(seconds=1)
        elif _purge_mode == "since":
            try:
                hh, mm = (int(x) for x in _purge_arg.split(":"))
                jst = timezone(timedelta(hours=9))
                now_jst = datetime.now(jst)
                after_dt = now_jst.replace(
                    hour=hh, minute=mm, second=0, microsecond=0
                )
                if after_dt > now_jst:  # time hasn't happened yet today -> yesterday
                    after_dt -= timedelta(days=1)
                after_dt -= timedelta(seconds=1)  # make the boundary inclusive
            except Exception:
                await message.reply(
                    "⚠️ 時刻は `!purge-since HH:MM`（JST・24h）で指定してください。例: `!purge-since 12:02`"
                )
                return True
        else:  # count
            count_target = min(int(_purge_arg), MAX_PURGE)

        deleted = 0
        if _purge_mode == "count":
            targets = []
            async for m in message.channel.history(limit=1000):
                if m.id == message.id:
                    continue  # skip the command message itself
                if _is_bot_msg(m):
                    targets.append(m)
                    if len(targets) >= count_target:
                        break
            for m in targets:
                try:
                    await m.delete()
                    deleted += 1
                except discord.errors.HTTPException:
                    pass
        elif not is_dm:
            # Guild text channel / thread: use purge(). Bulk delete needs
            # manage_messages; fall back to per-message delete if denied.
            _reason = f"purge requested by admin {message.author.id}"
            try:
                purged = await message.channel.purge(
                    after=after_dt, limit=MAX_PURGE, check=_is_bot_msg,
                    bulk=True, reason=_reason,
                )
            except discord.errors.Forbidden:
                purged = await message.channel.purge(
                    after=after_dt, limit=MAX_PURGE, check=_is_bot_msg,
                    bulk=False, reason=_reason,
                )
            deleted = len(purged)
        else:
            # DMs can't bulk-delete: iterate and delete own messages.
            async for m in message.channel.history(
                after=after_dt, limit=MAX_PURGE, oldest_first=True
            ):
                if _is_bot_msg(m):
                    try:
                        await m.delete()
                        deleted += 1
                    except discord.errors.HTTPException:
                        pass

        try:
            await message.delete()  # clean up the command message
        except discord.errors.HTTPException:
            pass
        log.info(
            "Purged %d bot message(s) (mode=%s, admin=%s)",
            deleted, _purge_mode, message.author.id,
        )
        note = await message.channel.send(
            f"🧹 かなえの投稿を **{deleted}件** 削除しました。"
        )
        await asyncio.sleep(5)
        try:
            await note.delete()
        except discord.errors.HTTPException:
            pass
    except discord.errors.Forbidden:
        await message.reply(
            "⚠️ メッセージを削除する権限がありません（read message history / manage messages を確認）。"
        )
    except Exception as e:
        log.warning("Failed to purge messages: %s", e, exc_info=True)
        await message.reply("❌ 一括削除に失敗しました。ログを確認してください。")
    return True

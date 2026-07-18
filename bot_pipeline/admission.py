"""Admission policy for Discord messages.

The stage owns only intake decisions and their immediate Discord responses.
All collaborators are supplied by the composition root so existing runtime
configuration and test patch points remain authoritative.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


log = logging.getLogger(__name__)

_ROLE_MENTION_TOKEN_RE = re.compile(r"<@&(\d+)>")


@dataclass(frozen=True)
class AttachmentLimits:
    image_content_types: frozenset[str] | set[str]
    text_content_types: frozenset[str] | set[str]
    max_image_bytes: int
    max_text_bytes: int
    max_arbitrary_bytes: int


@dataclass(frozen=True)
class AdmissionResult:
    caller_is_admin: bool
    is_dm: bool
    raw_user_text: str


@dataclass(frozen=True)
class AdmissionDeps:
    is_admin: Callable[[Any], bool]
    is_active_research_thread: Callable[[int], bool]
    new_request_id: Callable[[], str]
    get_request_id: Callable[[], str]
    guild_message_addresses_bot: Callable[[Any], bool]
    track_channel_message: Callable[[Any], None]
    handle_prefix_commands: Callable[..., Awaitable[bool]]
    strip_mention: Callable[[str, int], str]
    check_cooldown: Callable[[str, bool], tuple[bool, int]]
    check_daily_quota: Callable[[str, bool], tuple[bool, int]]
    daily_quota_reset_seconds: Callable[[str, bool], int]
    cap_text: Callable[[str, int], str]
    history_search_enabled: bool
    max_user_message_chars: int
    fmt_clock: Callable[[float], str] | None = None


def fmt_clock(epoch_s: float) -> str:
    """Return a local HH:MM label for user-facing rate-limit messages."""
    return time.strftime("%H:%M", time.localtime(epoch_s))


def _mb(size: int) -> str:
    return f"{size // (1024 * 1024)}MB"


def classify_attachment_rejections(
    attachments: list,
    *,
    caller_is_admin: bool,
    limits: AttachmentLimits,
) -> tuple[int, list[tuple[str, str]]]:
    """Predict attachment rejections without performing downloads."""
    accepted = 0
    rejected: list[tuple[str, str]] = []
    for attachment in attachments:
        name = getattr(attachment, "filename", None) or "attachment"
        size = getattr(attachment, "size", 0) or 0
        content_type = (
            (getattr(attachment, "content_type", None) or "")
            .split(";")[0]
            .strip()
            .lower()
        )
        if content_type in limits.image_content_types:
            if size > limits.max_image_bytes:
                rejected.append(
                    (name, f"サイズ超過(>{_mb(limits.max_image_bytes)})"),
                )
            else:
                accepted += 1
        elif content_type in limits.text_content_types:
            if size > limits.max_text_bytes:
                rejected.append((name, "サイズ超過(テキストは100KBまで)"))
            else:
                accepted += 1
        elif not caller_is_admin:
            rejected.append((name, "非対応の形式(管理者のみ対応)"))
        elif size > limits.max_arbitrary_bytes:
            rejected.append(
                (name, f"サイズ超過(>{_mb(limits.max_arbitrary_bytes)})"),
            )
        else:
            accepted += 1
    return accepted, rejected


def reply_ping_addresses_bot(
    message: Any,
    *,
    bot_user_id: int,
    forward_reference_type: Any,
) -> bool:
    """Return whether a Discord reply ping targets this bot identity."""
    reference = getattr(message, "reference", None)
    if reference is None or not getattr(reference, "message_id", None):
        return False
    if getattr(reference, "type", None) == forward_reference_type:
        return False

    referenced = getattr(reference, "resolved", None)
    referenced_author_id = getattr(
        getattr(referenced, "author", None),
        "id",
        None,
    )
    if referenced_author_id != bot_user_id:
        referenced = getattr(reference, "cached_message", None)
        referenced_author_id = getattr(
            getattr(referenced, "author", None),
            "id",
            None,
        )
    if referenced_author_id != bot_user_id:
        return False

    mentioned_user_ids = {
        user_id
        for user in (getattr(message, "mentions", ()) or ())
        if isinstance((user_id := getattr(user, "id", None)), int)
    }
    return bot_user_id in mentioned_user_ids


def guild_message_addresses_bot(
    message: Any,
    *,
    bot_user: Any,
    ignored_role_ids: set[int],
    bot_role_ids: set[int],
    bot_keywords: list[str] | tuple[str, ...] | set[str],
    forward_reference_type: Any,
) -> bool:
    """Return whether a guild message explicitly addresses this bot."""
    content = message.content or ""
    if bot_user is None:
        return False

    direct_mention = (
        re.search(
            rf"<@!?{re.escape(str(bot_user.id))}>",
            content,
        )
        is not None
    )
    mentioned_role_ids = {
        int(match.group(1)) for match in _ROLE_MENTION_TOKEN_RE.finditer(content)
    }
    for role in getattr(message, "role_mentions", ()) or ():
        role_id = getattr(role, "id", None)
        if isinstance(role_id, int):
            mentioned_role_ids.add(role_id)

    reply_mention = reply_ping_addresses_bot(
        message,
        bot_user_id=bot_user.id,
        forward_reference_type=forward_reference_type,
    )
    if direct_mention or reply_mention:
        return True

    ignored_role_mentions = mentioned_role_ids & ignored_role_ids
    own_role_mentions = mentioned_role_ids & bot_role_ids
    if own_role_mentions - ignored_role_mentions:
        return True
    if ignored_role_mentions:
        return False

    content_lower = content.lower()
    return any(keyword in content_lower for keyword in bot_keywords)


async def admit_message(
    message: Any,
    *,
    bot: Any,
    dm_channel_types: tuple[type, ...],
    deps: AdmissionDeps,
) -> AdmissionResult | None:
    """Apply intake gates and return normalized caller input when admitted."""
    if message.author == bot.user or message.author.bot:
        return None
    if deps.is_active_research_thread(message.channel.id):
        return None

    deps.new_request_id()
    log.debug(
        "[rid=%s] [%s] %s: %s",
        deps.get_request_id(),
        message.channel,
        message.author,
        message.content[:200],
    )

    is_dm = isinstance(message.channel, dm_channel_types)
    if is_dm and not deps.is_admin(message.author):
        return None
    if not is_dm and not deps.guild_message_addresses_bot(message):
        if deps.history_search_enabled:
            deps.track_channel_message(message)
        return None

    caller_is_admin = deps.is_admin(message.author)
    content_stripped = message.content.strip().lower()
    bare_text = (
        deps.strip_mention(message.content, bot.user.id).strip().lower()
        if not is_dm
        else content_stripped
    )
    if await deps.handle_prefix_commands(
        message,
        bot,
        bare_text=bare_text,
        is_dm=is_dm,
        caller_is_admin=caller_is_admin,
    ):
        return None

    if not caller_is_admin:
        cooldown_allowed, cooldown_wait = deps.check_cooldown(
            str(message.author.id),
            caller_is_admin,
        )
        if not cooldown_allowed:
            await message.reply(
                f"⏳ クールダウン中です。あと{cooldown_wait}秒"
                f"({(deps.fmt_clock or fmt_clock)(time.time() + cooldown_wait)} "
                "頃に解除)で送信できます。",
            )
            return None

        quota_allowed, _ = deps.check_daily_quota(
            str(message.author.id),
            caller_is_admin,
        )
        if not quota_allowed:
            quota_msg = "⏳ 今日の利用上限に達しました。"
            reset_s = deps.daily_quota_reset_seconds(
                str(message.author.id),
                caller_is_admin,
            )
            if reset_s > 0:
                reset_at = time.time() + reset_s
                quota_msg += (
                    f"{time.strftime('%m/%d %H:%M', time.localtime(reset_at))}"
                    " 頃に回復します。"
                )
            else:
                quota_msg += "また明日来てください。"
            await message.reply(quota_msg)
            return None

    raw_user_text = (
        message.content.strip()
        if is_dm
        else deps.strip_mention(message.content, bot.user.id)
    )
    raw_user_text = deps.cap_text(
        raw_user_text,
        deps.max_user_message_chars,
    )
    if not raw_user_text:
        await message.reply(
            "何か用ですか？"
            if is_dm
            else "Please include a message after mentioning me!",
        )
        return None

    return AdmissionResult(
        caller_is_admin=caller_is_admin,
        is_dm=is_dm,
        raw_user_text=raw_user_text,
    )

"""Discord message processing utilities — matches free-code utils/messages.ts pattern."""

import re
import logging
import time
from collections import OrderedDict
from urllib.parse import urlparse

import discord

from config import (
    ADMIN_USER_IDS,
    BOT_KEYWORDS,
    BOT_ROLE_IDS,
    MAX_RESOLVED_LINKS,
    THREAD_CONTEXT_CACHE_MAX,
    THREAD_CONTEXT_CACHE_TTL_S,
    THREAD_HISTORY_LIMIT,
)

log = logging.getLogger(__name__)

# TTL + LRU cache: (channel_id, before_message_id) -> (expiry_ts, text)
# Keeps brief conversational bursts fast without staleness risk.
_thread_context_cache: OrderedDict[tuple[int, int], tuple[float, str]] = OrderedDict()


def _cache_get(key: tuple[int, int]) -> str | None:
    entry = _thread_context_cache.get(key)
    if entry is None:
        return None
    expiry, text = entry
    if time.monotonic() >= expiry:
        _thread_context_cache.pop(key, None)
        return None
    _thread_context_cache.move_to_end(key)
    return text


def _cache_set(key: tuple[int, int], text: str) -> None:
    _thread_context_cache[key] = (time.monotonic() + THREAD_CONTEXT_CACHE_TTL_S, text)
    _thread_context_cache.move_to_end(key)
    while len(_thread_context_cache) > THREAD_CONTEXT_CACHE_MAX:
        _thread_context_cache.popitem(last=False)

# ---------------------------------------------------------------------------
# Mention stripping (pre-compiled regex cache)
# ---------------------------------------------------------------------------
_mention_re_cache: re.Pattern | None = None
_keyword_re_cache: list[re.Pattern] = []


def build_mention_regex(user_id: int) -> None:
    """Pre-compile mention/keyword regex patterns. Called once after on_ready."""
    global _mention_re_cache, _keyword_re_cache
    parts = [rf"<@!?{user_id}>"]
    for role_id in BOT_ROLE_IDS:
        parts.append(rf"<@&{role_id}>")
    _mention_re_cache = re.compile(r"(?:" + "|".join(parts) + r")\s*")
    _keyword_re_cache = [re.compile(rf"(?i){re.escape(kw)}[,、]?\s*") for kw in BOT_KEYWORDS]


def strip_mention(content: str, user_id: int) -> str:
    """Remove the bot's direct mentions, role mentions, and keywords."""
    text = _mention_re_cache.sub("", content) if _mention_re_cache else content
    for pat in _keyword_re_cache:
        text = pat.sub("", text, count=1)
    return text.strip()


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------
def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split text into chunks that fit within Discord's message limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


# ---------------------------------------------------------------------------
# Embed formatting
# ---------------------------------------------------------------------------
def format_embed(embed: discord.Embed) -> str:
    parts: list[str] = []
    if embed.title:
        parts.append(f"**{embed.title}**")
    if embed.description:
        parts.append(embed.description)
    for field in embed.fields:
        parts.append(f"__{field.name}__: {field.value}")
    if embed.footer and embed.footer.text:
        parts.append(f"({embed.footer.text})")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Thread context (free-code: initialMessages from history)
# ---------------------------------------------------------------------------
MAX_CONTEXT_CHARS = 8000


def _should_include_msg(msg: discord.Message, caller_is_admin: bool) -> bool:
    """Filter rule for context fetchers.

    Non-admin callers must NOT see admin-authored messages — admins may have
    pasted secrets or command output, and the bot must not launder that back
    into a non-admin's prompt.
    """
    if caller_is_admin:
        return True
    return msg.author.id not in ADMIN_USER_IDS


async def fetch_thread_context(
    thread: discord.Thread,
    before_message: discord.Message,
    *,
    caller_is_admin: bool = False,
) -> str:
    """Fetch past messages in a thread and format them inline (capped at MAX_CONTEXT_CHARS)."""
    # Cache is partitioned on caller_is_admin so an admin view never leaks
    # into a non-admin caller via a shared cache entry.
    cache_key = (thread.id, before_message.id, bool(caller_is_admin))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    lines: list[str] = []
    count = 0
    total_chars = 0
    async for msg in thread.history(limit=THREAD_HISTORY_LIMIT, before=before_message, oldest_first=True):
        if not _should_include_msg(msg, caller_is_admin):
            continue
        count += 1
        author = msg.author.display_name
        content = msg.content or ""
        extras: list[str] = []
        for embed in msg.embeds:
            formatted = format_embed(embed)
            if formatted:
                extras.append(formatted)
        for att in msg.attachments:
            extras.append(f"[Attachment: {att.filename}]")
        body = content + ("\n" + "\n".join(extras) if extras else "")
        if body.strip():
            line = f"{author}: {body.strip()}"
            if total_chars + len(line) > MAX_CONTEXT_CHARS:
                lines.append("(...truncated...)")
                break
            total_chars += len(line)
            lines.append(line)

    if not lines:
        _cache_set(cache_key, "")
        return ""
    result = f"[Thread: {thread.name} - past {count} messages]\n" + "\n".join(lines) + "\n[End of thread history]"
    _cache_set(cache_key, result)
    return result


CHANNEL_CONTEXT_LIMIT = 10


async def fetch_channel_context(
    channel,
    before_message: discord.Message,
    *,
    caller_is_admin: bool = False,
) -> str:
    """Fetch recent messages in a regular channel (capped at MAX_CONTEXT_CHARS)."""
    cache_key = (channel.id, before_message.id, bool(caller_is_admin))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    lines: list[str] = []
    total_chars = 0
    count = 0
    async for msg in channel.history(limit=CHANNEL_CONTEXT_LIMIT, before=before_message):
        if not _should_include_msg(msg, caller_is_admin):
            continue
        count += 1
        author = msg.author.display_name
        content = msg.content or ""
        extras: list[str] = []
        for embed in msg.embeds:
            formatted = format_embed(embed)
            if formatted:
                extras.append(formatted)
        for att in msg.attachments:
            extras.append(f"[Attachment: {att.filename}]")
        body = content + ("\n" + "\n".join(extras) if extras else "")
        if body.strip():
            line = f"{author}: {body.strip()}"
            if total_chars + len(line) > MAX_CONTEXT_CHARS:
                lines.append("(...truncated...)")
                break
            total_chars += len(line)
            lines.append(line)

    if not lines:
        _cache_set(cache_key, "")
        return ""
    lines.reverse()
    result = f"[Channel recent {count} messages]\n" + "\n".join(lines) + "\n[End of channel history]"
    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Message link resolution
# ---------------------------------------------------------------------------
# Strict scheme + host match. The regex intentionally does NOT match
# `https://discord.com@evil/channels/...` because `urlparse` will see the
# host as `evil`, and we verify the parsed host below before dereferencing.
_ALLOWED_DISCORD_HOSTS = frozenset({
    "discord.com",
    "discordapp.com",
    "ptb.discord.com",
    "canary.discord.com",
})
DISCORD_MSG_URL_RE = re.compile(
    r"https?://[\w.@:]*?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def _parse_and_verify_url(raw_url: str) -> tuple[int, int, int] | None:
    """Parse a Discord message URL and return (guild_id, channel_id, message_id).

    Returns None if the URL is malformed, the host is not an allow-listed
    Discord host, or the path doesn't look like /channels/<id>/<id>/<id>.
    This guards against `https://discord.com@evil.com/channels/...` style
    userinfo-spoofing attacks.
    """
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_DISCORD_HOSTS:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "channels":
        return None
    try:
        return int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None


def _requester_can_read(channel: discord.abc.Messageable, requester) -> bool:
    """Confused-deputy guard: refuse to read a channel the requester can't.

    The bot may have joined admin-only channels the attacker cannot see. If
    we blindly fetched messages we'd be a lifted-privilege oracle. Require
    the requester to be a guild member with `read_message_history`.
    """
    guild = getattr(channel, "guild", None)
    if guild is None:
        # DM / group DM: only allow if requester is the channel's recipient.
        # discord.DMChannel has `.recipient`, GroupChannel has `.recipients`.
        recipient = getattr(channel, "recipient", None)
        recipients = getattr(channel, "recipients", None) or ([] if recipient is None else [recipient])
        return any(getattr(r, "id", None) == requester.id for r in recipients)
    member = guild.get_member(requester.id)
    if member is None:
        return False
    perms_for = getattr(channel, "permissions_for", None)
    if perms_for is None:
        return False
    try:
        perms = perms_for(member)
    except Exception:
        return False
    return bool(perms.read_message_history and perms.view_channel)


async def _resolve_one_link(
    discord_client: discord.Client,
    match: re.Match,
    requester,
) -> tuple[re.Match, str | None]:
    verified = _parse_and_verify_url(match.group(0))
    if verified is None:
        return match, None
    guild_id, channel_id, message_id = verified
    try:
        channel = discord_client.get_channel(channel_id) or await discord_client.fetch_channel(channel_id)
        if channel is None:
            return match, None
        # Guild-id sanity check: the URL's guild must match the fetched channel's guild.
        channel_guild = getattr(channel, "guild", None)
        if channel_guild is not None and channel_guild.id != guild_id:
            log.warning(
                "Message link guild mismatch: url=%d fetched=%d", guild_id, channel_guild.id,
            )
            return match, None
        # Authorization: the requester must be able to read the target channel.
        if requester is not None and not _requester_can_read(channel, requester):
            log.warning(
                "Rejecting message-link resolution: user=%s lacks read access to channel=%s",
                getattr(requester, "id", "?"), channel_id,
            )
            return match, None

        msg = await channel.fetch_message(message_id)

        parts: list[str] = [f"[Referenced message by {msg.author.display_name} in #{getattr(channel, 'name', channel_id)}]"]
        if msg.content:
            parts.append(msg.content)
        for embed in msg.embeds:
            formatted = format_embed(embed)
            if formatted:
                parts.append(formatted)
        for attachment in msg.attachments:
            parts.append(f"[Attachment: {attachment.filename}]")

        log.info("Resolved message link: guild=%s ch=%s msg=%s", guild_id, channel_id, message_id)
        return match, "\n".join(parts)
    except Exception as e:
        log.warning("Failed to resolve message link %s: %s", match.group(0), e)
        return match, None


async def resolve_message_links(
    discord_client: discord.Client,
    text: str,
    *,
    requester=None,
) -> str:
    """Find Discord message URLs in text, fetch content in parallel, and inline them.

    `requester` should be the user whose message contained the link; if set,
    each target channel is checked for that user's read permission before
    dereferencing (prevents confused-deputy leaks).
    """
    import asyncio
    matches = list(DISCORD_MSG_URL_RE.finditer(text))
    if not matches:
        return text

    matches = matches[:MAX_RESOLVED_LINKS]
    results = await asyncio.gather(*[
        _resolve_one_link(discord_client, m, requester) for m in matches
    ])
    for match, replacement in reversed(results):
        if replacement is not None:
            text = text[:match.start()] + replacement + text[match.end():]
    return text

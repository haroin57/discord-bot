"""Discord message processing utilities — matches free-code utils/messages.ts pattern."""

import re
import logging
from urllib.parse import urlparse

import aiohttp
import discord

from utils.cache import TTLLRUCache

from config import (
    ADMIN_USER_IDS,
    ALLOWED_ATTACHMENT_HOSTS,
    BOT_KEYWORDS,
    BOT_ROLE_IDS,
    CROSS_THREAD_CONTEXT_MESSAGES,
    MAX_CROSS_THREAD_CHARS,
    MAX_CROSS_THREAD_CONTEXTS,
    MAX_RESOLVED_LINKS,
    THREAD_CONTEXT_CACHE_MAX,
    THREAD_CONTEXT_CACHE_TTL_S,
    THREAD_HISTORY_LIMIT,
)

log = logging.getLogger(__name__)

# TTL + LRU cache: (channel_id, before_message_id) -> text. Keeps brief
# conversational bursts fast without staleness risk. Implementation lives
# in utils.cache so it can be reused for other caches in the future.
_thread_context_cache: TTLLRUCache[tuple[int, int], str] = TTLLRUCache(
    maxsize=THREAD_CONTEXT_CACHE_MAX,
    ttl_s=THREAD_CONTEXT_CACHE_TTL_S,
)


def _cache_get(key: tuple[int, int]) -> str | None:
    return _thread_context_cache.get(key)


def _cache_set(key: tuple[int, int], text: str) -> None:
    _thread_context_cache.set(key, text)

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
MAX_CONTEXT_CHARS = 20000

# 3-layer context hierarchy
LAYER1_COUNT = 15      # Recent messages: full text
LAYER2_COUNT = 10      # Earlier messages: summarized
LAYER2_MAX_CHARS = 150 # Max chars per Layer 2 message


def _summarize_message(body: str, max_chars: int = LAYER2_MAX_CHARS) -> str:
    """Layer 2: truncate message to first max_chars characters."""
    if len(body) <= max_chars:
        return body
    return body[:max_chars] + " [...]"


def _should_include_msg(msg: discord.Message, caller_is_admin: bool) -> bool:
    """Filter rule for context fetchers.

    Non-admin callers must NOT see admin-authored messages — admins may have
    pasted secrets or command output, and the bot must not launder that back
    into a non-admin's prompt.
    """
    if caller_is_admin:
        return True
    return msg.author.id not in ADMIN_USER_IDS


def _format_msg_body(msg: discord.Message) -> str:
    """Extract full body text from a Discord message (content + embeds + attachments)."""
    content = msg.content or ""
    extras: list[str] = []
    for embed in msg.embeds:
        formatted = format_embed(embed)
        if formatted:
            extras.append(formatted)
    for att in msg.attachments:
        extras.append(f"[Attachment: {att.filename}]")
    body = content + ("\n" + "\n".join(extras) if extras else "")
    return body.strip()


def _build_layered_lines(
    messages: list[tuple[str, str]],
    *,
    oldest_first: bool = True,
) -> tuple[list[str], int]:
    """Build layered context lines from (author, body) pairs.

    Messages must be in chronological order (oldest first).
    Returns (lines, total_chars).

    Layer 1 (last LAYER1_COUNT): full text
    Layer 2 (preceding LAYER2_COUNT): summarized
    """
    total = len(messages)
    layer2_start = max(0, total - LAYER1_COUNT - LAYER2_COUNT)
    layer1_start = max(0, total - LAYER1_COUNT)

    layer2_lines: list[str] = []
    layer1_lines: list[str] = []
    total_chars = 0

    for i, (author, body) in enumerate(messages):
        if not body:
            continue
        if i < layer2_start:
            # Layer 3: skip (Mem0 covers)
            continue
        elif i < layer1_start:
            # Layer 2: summarized
            line = f"{author}: {_summarize_message(body)}"
            if total_chars + len(line) > MAX_CONTEXT_CHARS:
                layer2_lines.append("(...truncated...)")
                break
            total_chars += len(line)
            layer2_lines.append(line)
        else:
            # Layer 1: full text
            line = f"{author}: {body}"
            if total_chars + len(line) > MAX_CONTEXT_CHARS:
                layer1_lines.append("(...truncated...)")
                break
            total_chars += len(line)
            layer1_lines.append(line)

    lines: list[str] = []
    if layer2_lines:
        lines.append("--- earlier (summary) ---")
        lines.extend(layer2_lines)
    if layer1_lines:
        if layer2_lines:
            lines.append("--- recent (full) ---")
        lines.extend(layer1_lines)

    return lines, total_chars


async def fetch_thread_context(
    thread: discord.Thread,
    before_message: discord.Message,
    *,
    caller_is_admin: bool = False,
) -> str:
    """Fetch past messages in a thread with layered context (capped at MAX_CONTEXT_CHARS).

    Layer 1 (last 3 messages): full text for conversational continuity.
    Layer 2 (messages 4-15): summarized to ~150 chars each.
    Layer 3 (16+): omitted — Mem0 long-term memory covers older context.
    """
    cache_key = (thread.id, before_message.id, bool(caller_is_admin))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Collect messages in chronological order (oldest_first=True)
    collected: list[tuple[str, str]] = []
    async for msg in thread.history(
        limit=LAYER1_COUNT + LAYER2_COUNT,
        before=before_message,
        oldest_first=True,
    ):
        if not _should_include_msg(msg, caller_is_admin):
            continue
        body = _format_msg_body(msg)
        if body:
            collected.append((msg.author.display_name, body))

    if not collected:
        _cache_set(cache_key, "")
        return ""

    lines, _ = _build_layered_lines(collected, oldest_first=True)
    result = (
        f"[Thread: {thread.name} - past {len(collected)} messages]\n"
        + "\n".join(lines)
        + "\n[End of thread history]"
    )
    _cache_set(cache_key, result)
    return result


CHANNEL_CONTEXT_LIMIT = LAYER1_COUNT + LAYER2_COUNT  # 15


async def fetch_channel_context(
    channel,
    before_message: discord.Message,
    *,
    caller_is_admin: bool = False,
) -> str:
    """Fetch recent messages in a channel with layered context (capped at MAX_CONTEXT_CHARS).

    Layer 1 (last 3 messages): full text for conversational continuity.
    Layer 2 (messages 4-15): summarized to ~150 chars each.
    Layer 3 (16+): omitted — Mem0 long-term memory covers older context.
    """
    cache_key = (channel.id, before_message.id, bool(caller_is_admin))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # channel.history default is newest-first, collect then reverse
    collected_rev: list[tuple[str, str]] = []
    async for msg in channel.history(limit=CHANNEL_CONTEXT_LIMIT, before=before_message):
        if not _should_include_msg(msg, caller_is_admin):
            continue
        body = _format_msg_body(msg)
        if body:
            collected_rev.append((msg.author.display_name, body))

    if not collected_rev:
        _cache_set(cache_key, "")
        return ""

    # Reverse to chronological order for layering
    collected_rev.reverse()
    lines, _ = _build_layered_lines(collected_rev, oldest_first=True)
    result = (
        f"[Channel recent {len(collected_rev)} messages]\n"
        + "\n".join(lines)
        + "\n[End of channel history]"
    )
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
# Text content types to download inline when resolving message links.
_INLINE_TEXT_TYPES = frozenset({
    "text/plain", "text/csv", "text/markdown", "text/html", "text/xml",
    "application/json", "application/xml", "application/javascript",
})
_MAX_LINK_TEXT_BYTES = 100 * 1024  # 100KB cap

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
    *,
    caller_is_admin: bool = False,
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
        # Admin users bypass this check — they have full access by definition.
        if not caller_is_admin and requester is not None and not _requester_can_read(channel, requester):
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
        # Download text attachments inline (e.g. message.txt from long Discord messages)
        for attachment in msg.attachments:
            content_type = (attachment.content_type or "").split(";")[0].strip().lower()
            if content_type in _INLINE_TEXT_TYPES and (attachment.size or 0) <= _MAX_LINK_TEXT_BYTES:
                # SSRF guard mirrored from utils/attachments._host_is_allowed.
                # Discord occasionally serves attachments from unexpected
                # hosts (or a crafted message-link target could be spoofed
                # via CDN redirect quirks); refuse anything that isn't a
                # Discord CDN https URL before issuing the request.
                attachment_url = attachment.url or ""
                allowed = False
                try:
                    parsed = urlparse(attachment_url)
                    if parsed.scheme == "https" and (parsed.hostname or "").lower() in ALLOWED_ATTACHMENT_HOSTS:
                        allowed = True
                except ValueError:
                    allowed = False
                if not allowed:
                    log.warning(
                        "Rejecting linked attachment from non-allowlisted host: %s",
                        attachment_url,
                    )
                else:
                    try:
                        async with aiohttp.ClientSession() as sess:
                            async with sess.get(
                                attachment_url, allow_redirects=False
                            ) as resp:
                                if resp.status == 200:
                                    # Cap by Content-Length up front so we don't
                                    # buffer the entire response just to discard.
                                    cl = resp.headers.get("Content-Length")
                                    if cl is not None:
                                        try:
                                            if int(cl) > _MAX_LINK_TEXT_BYTES:
                                                parts.append(f"[Attachment: {attachment.filename}]")
                                                continue
                                        except ValueError:
                                            pass
                                    data = b""
                                    async for chunk in resp.content.iter_chunked(64 * 1024):
                                        data += chunk
                                        if len(data) > _MAX_LINK_TEXT_BYTES:
                                            data = b""
                                            break
                                    if 0 < len(data) <= _MAX_LINK_TEXT_BYTES:
                                        try:
                                            text = data.decode("utf-8")
                                        except UnicodeDecodeError:
                                            text = data.decode("latin-1")
                                        parts.append(f"[Attachment: {attachment.filename}]\n{text}")
                                        continue
                    except Exception as e:
                        log.warning(
                            "Failed to download linked attachment %s: %s",
                            attachment.filename, e,
                        )
            parts.append(f"[Attachment: {attachment.filename}]")

        # If the resolved message is in a thread, fetch surrounding context
        if isinstance(channel, discord.Thread):
            try:
                thread_ctx_lines: list[str] = []
                # Fetch messages around the referenced message
                before_msgs: list[tuple[str, str]] = []
                async for m in channel.history(
                    limit=CROSS_THREAD_CONTEXT_MESSAGES,
                    before=msg,
                    oldest_first=False,
                ):
                    if requester is not None and not _requester_can_read(channel, requester):
                        continue
                    body = _format_msg_body(m)
                    if body:
                        before_msgs.append((m.author.display_name, body))
                before_msgs.reverse()  # chronological order

                after_msgs: list[tuple[str, str]] = []
                async for m in channel.history(
                    limit=CROSS_THREAD_CONTEXT_MESSAGES,
                    after=msg,
                    oldest_first=True,
                ):
                    if requester is not None and not _requester_can_read(channel, requester):
                        continue
                    body = _format_msg_body(m)
                    if body:
                        after_msgs.append((m.author.display_name, body))

                all_ctx = before_msgs + [(msg.author.display_name, _format_msg_body(msg))] + after_msgs
                ctx_lines, _ = _build_layered_lines(all_ctx, oldest_first=True)
                if ctx_lines:
                    parts.append(
                        f"\n[Thread context from #{getattr(channel, 'name', channel_id)} "
                        f"({len(all_ctx)} messages around referenced)]\n"
                        + "\n".join(ctx_lines)
                        + "\n[End of cross-thread context]"
                    )
                log.info("Cross-thread context fetched: thread=%s msgs=%d", channel.name, len(all_ctx))
            except Exception as e:
                log.warning("Failed to fetch cross-thread context for %s: %s", channel_id, e)

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
    caller_is_admin: bool = False,
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
        _resolve_one_link(discord_client, m, requester, caller_is_admin=caller_is_admin)
        for m in matches
    ])
    for match, replacement in reversed(results):
        if replacement is not None:
            text = text[:match.start()] + replacement + text[match.end():]
    return text


# ---------------------------------------------------------------------------
# Channel mention → cross-thread context resolution
# ---------------------------------------------------------------------------
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")


async def resolve_channel_mention_contexts(
    discord_client: discord.Client,
    text: str,
    *,
    current_channel_id: int,
    requester=None,
    caller_is_admin: bool = False,
) -> str:
    """Detect <#channel_id> mentions that point to threads and fetch their context.

    Returns a formatted string of cross-thread contexts (empty string if none).
    Skips the current channel/thread to avoid duplication.
    """
    import asyncio

    mentions = _CHANNEL_MENTION_RE.findall(text)
    if not mentions:
        return ""

    # Deduplicate and exclude current channel
    seen: set[int] = set()
    channel_ids: list[int] = []
    for cid_str in mentions:
        cid = int(cid_str)
        if cid == current_channel_id or cid in seen:
            continue
        seen.add(cid)
        channel_ids.append(cid)

    channel_ids = channel_ids[:MAX_CROSS_THREAD_CONTEXTS]
    if not channel_ids:
        return ""

    async def _fetch_one(cid: int) -> str | None:
        try:
            channel = discord_client.get_channel(cid) or await discord_client.fetch_channel(cid)
            if channel is None:
                return None
            if not isinstance(channel, discord.Thread):
                return None
            # Permission check
            if requester is not None and not _requester_can_read(channel, requester):
                log.warning(
                    "Rejecting channel-mention context: user=%s lacks access to thread=%s",
                    getattr(requester, "id", "?"), cid,
                )
                return None

            collected: list[tuple[str, str]] = []
            async for msg in channel.history(
                limit=CROSS_THREAD_CONTEXT_MESSAGES * 2,
                oldest_first=False,
            ):
                if not _should_include_msg(msg, caller_is_admin):
                    continue
                body = _format_msg_body(msg)
                if body:
                    collected.append((msg.author.display_name, body))
            if not collected:
                return None

            collected.reverse()  # chronological
            lines, _ = _build_layered_lines(collected, oldest_first=True)
            result = (
                f"[Cross-thread context: #{channel.name} ({len(collected)} recent messages)]\n"
                + "\n".join(lines)
                + "\n[End of cross-thread context]"
            )
            if len(result) > MAX_CROSS_THREAD_CHARS:
                result = result[:MAX_CROSS_THREAD_CHARS] + "\n[...truncated...]"
            log.info("Channel-mention context fetched: thread=%s msgs=%d", channel.name, len(collected))
            return result
        except Exception as e:
            log.warning("Failed to fetch channel-mention context for %s: %s", cid, e)
            return None

    results = await asyncio.gather(*[_fetch_one(cid) for cid in channel_ids])
    contexts = [r for r in results if r]
    return "\n\n".join(contexts)

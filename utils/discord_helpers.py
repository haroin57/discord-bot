"""Discord message processing utilities — matches free-code utils/messages.ts pattern."""

import re
import logging
from collections.abc import Collection
from urllib.parse import urlparse

import aiohttp
import discord

from utils.cache import TTLLRUCache
from utils import context_gateway

from config import (
    ADMIN_USER_IDS,
    ALLOWED_ATTACHMENT_HOSTS,
    BOT_IGNORED_ROLE_IDS,
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
# NOTE: BOT_KEYWORDS ("かなえ" 等) は意図的に strip しない。キーワード除去は
# 本文途中の「かなえ」(例:「かなえちゃんによると」)にマッチして後続を食い、
# 本文を破壊する事故源だった (2026-06-08, ADR-0003)。呼びかけキーワードは
# prompt 本文に残し、Claude 側に自然な文脈として渡す。strip するのは機械的な
# メンショントークン (<@id> / <@&role>) のみ。
_mention_re_cache: re.Pattern | None = None


def build_mention_regex(user_id: int) -> None:
    """Pre-compile the bot's mention/role-mention regex. Called once after on_ready."""
    global _mention_re_cache
    parts = [rf"<@!?{user_id}>"]
    for role_id in BOT_ROLE_IDS | BOT_IGNORED_ROLE_IDS:
        parts.append(rf"<@&{role_id}>")
    _mention_re_cache = re.compile(r"(?:" + "|".join(parts) + r")\s*")


def strip_mention(content: str, user_id: int) -> str:
    """Remove the bot's direct mentions and role mentions. Keywords are preserved."""
    text = _mention_re_cache.sub("", content) if _mention_re_cache else content
    return text.strip()


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------
# A fence line: up to 3 leading spaces, ``` (or longer), optional language tag.
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,})(\S*)\s*$")
# Room reserved per chunk for the "\n-# (xx/xx)" part marker.
_PART_MARKER_RESERVE = 12


def split_message(text: str, limit: int = 2000, *, numbered: bool = True) -> list[str]:
    """Split text into chunks that fit within Discord's message limit.

    Code-fence aware: a chunk never ends inside an unbalanced ``` fence.
    When a fence has to span chunks it is closed at the end of the current
    chunk and reopened (same language tag) at the start of the next, so
    every chunk renders correctly on its own. With ``numbered`` (default),
    multi-chunk output gets a small ``-# (i/n)`` subtext marker appended to
    each chunk; single-chunk output is returned untouched.
    """
    if len(text) <= limit:
        return [text]
    reserve = _PART_MARKER_RESERVE if numbered else 0
    chunks = _split_fence_aware(text, max(limit - reserve, 64))
    if numbered and len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{c}\n-# ({i}/{total})" for i, c in enumerate(chunks, 1)]
    return chunks


def _split_fence_aware(text: str, limit: int) -> list[str]:
    """Line-based splitter that keeps ``` fences balanced per chunk."""
    # Pre-split any single line longer than a chunk (space-preferring, then
    # hard) so the main loop only ever handles lines that fit. The -8 margin
    # leaves room for a "\n```" fence close on fenced chunks.
    line_cap = limit - 8
    lines: list[str] = []
    for raw in text.split("\n"):
        while len(raw) > line_cap:
            cut = raw.rfind(" ", 0, line_cap)
            if cut <= 0:
                cut = line_cap
            lines.append(raw[:cut])
            raw = raw[cut:].lstrip(" ")
        lines.append(raw)

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0  # == len("\n".join(cur))
    fence_open: str | None = None  # opening fence line, e.g. "```python"

    def flush() -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        body = "\n".join(cur)
        if fence_open:
            body += "\n```"
        chunks.append(body)
        cur = []
        cur_len = 0
        if fence_open:
            cur.append(fence_open)
            cur_len = len(fence_open)

    for line in lines:
        sep = 1 if cur else 0
        close_margin = 4 if fence_open else 0  # "\n```"
        if cur_len + sep + len(line) + close_margin > limit:
            flush()
            sep = 1 if cur else 0
        cur.append(line)
        cur_len += sep + len(line)
        if _FENCE_RE.match(line):
            fence_open = None if fence_open else (line.strip() or "```")

    # Final chunk: emitted as-is — if the source text itself left a fence
    # unclosed, that is the author's content, not a split artifact.
    if cur:
        chunks.append("\n".join(cur))
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
LAYER1_COUNT = 25  # Recent messages: full text
LAYER2_COUNT = 15  # Earlier messages: summarized
LAYER2_MAX_CHARS = 150  # Max chars per Layer 2 message


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
    bot_user_id: int | None = None,
    admin_user_ids: Collection[int] = ADMIN_USER_IDS,
) -> str:
    """Fetch past messages in a thread with layered context (capped at MAX_CONTEXT_CHARS).

    Layer 1 (last 3 messages): full text for conversational continuity.
    Layer 2 (messages 4-15): summarized to ~150 chars each.
    Layer 3 (16+): omitted — Mem0 long-term memory covers older context.
    """
    return await context_gateway.fetch_thread_context(
        thread,
        before_message,
        caller_is_admin=caller_is_admin,
        bot_user_id=bot_user_id,
        admin_user_ids=admin_user_ids,
        layer1_count=LAYER1_COUNT,
        layer2_count=LAYER2_COUNT,
        cache_get=_cache_get,
        cache_set=_cache_set,
        should_include=_should_include_msg,
        format_body=_format_msg_body,
        build_lines=_build_layered_lines,
        max_context_chars=MAX_CONTEXT_CHARS,
    )


CHANNEL_CONTEXT_LIMIT = LAYER1_COUNT + LAYER2_COUNT  # 40


async def fetch_channel_context(
    channel,
    before_message: discord.Message,
    *,
    caller_is_admin: bool = False,
    bot_user_id: int | None = None,
    admin_user_ids: Collection[int] = ADMIN_USER_IDS,
) -> str:
    """Fetch recent messages in a channel with layered context (capped at MAX_CONTEXT_CHARS).

    Layer 1 (last 3 messages): full text for conversational continuity.
    Layer 2 (messages 4-15): summarized to ~150 chars each.
    Layer 3 (16+): omitted — Mem0 long-term memory covers older context.
    """
    return await context_gateway.fetch_channel_context(
        channel,
        before_message,
        caller_is_admin=caller_is_admin,
        bot_user_id=bot_user_id,
        admin_user_ids=admin_user_ids,
        context_limit=CHANNEL_CONTEXT_LIMIT,
        cache_get=_cache_get,
        cache_set=_cache_set,
        should_include=_should_include_msg,
        format_body=_format_msg_body,
        build_lines=_build_layered_lines,
        max_context_chars=MAX_CONTEXT_CHARS,
    )


# ---------------------------------------------------------------------------
# Message link resolution
# ---------------------------------------------------------------------------
# Strict scheme + host match. The regex intentionally does NOT match
# `https://discord.com@evil/channels/...` because `urlparse` will see the
# host as `evil`, and we verify the parsed host below before dereferencing.
_ALLOWED_DISCORD_HOSTS = frozenset(
    {
        "discord.com",
        "discordapp.com",
        "ptb.discord.com",
        "canary.discord.com",
    }
)
# Text content types to download inline when resolving message links.
_INLINE_TEXT_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/markdown",
        "text/html",
        "text/xml",
        "application/json",
        "application/xml",
        "application/javascript",
    }
)
_MAX_LINK_TEXT_BYTES = 100 * 1024  # 100KB cap

DISCORD_MSG_URL_RE = re.compile(
    r"https?://[\w.@:]*?discord(?:app)?\.com/channels/"
    r"(@me|\d+)/(\d+)/(\d+)(?![A-Za-z0-9_/%-])"
)


def _parse_and_verify_url(raw_url: str) -> tuple[int | str, int, int] | None:
    """Return ``(guild_scope, channel_id, message_id)`` for a Discord URL.

    Returns None if the URL is malformed, the host is not an allow-listed
    Discord host, or the path doesn't look like /channels/<id>/<id>/<id>.
    ``guild_scope`` is an integer guild ID or the literal ``"@me"`` for a
    direct-message link.
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
    if len(parts) != 4 or parts[0] != "channels":
        return None
    try:
        guild_scope: int | str = (
            "@me" if parts[1] == "@me" else int(parts[1])
        )
        return guild_scope, int(parts[2]), int(parts[3])
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
        recipients = getattr(channel, "recipients", None) or (
            [] if recipient is None else [recipient]
        )
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
    guild_scope, channel_id, message_id = verified
    try:
        channel = discord_client.get_channel(
            channel_id
        ) or await discord_client.fetch_channel(channel_id)
        if channel is None:
            return match, None
        # Scope sanity check: @me links must resolve to a private channel, and
        # numeric guild links must not cross into a different guild.
        channel_guild = getattr(channel, "guild", None)
        if guild_scope == "@me" and channel_guild is not None:
            log.warning(
                "Message link scope mismatch: url=@me fetched guild=%s",
                channel_guild.id,
            )
            return match, None
        if guild_scope != "@me" and (
            channel_guild is None or channel_guild.id != guild_scope
        ):
            log.warning(
                "Message link guild mismatch: url=%s fetched=%s",
                guild_scope,
                getattr(channel_guild, "id", None),
            )
            return match, None
        # Authorization: DM membership is always enforced, including for an
        # administrator. An admin may manage every guild channel visible to
        # the bot, but that must not expose the bot's DM with another user.
        # Guild links retain the existing admin bypass. Public bot call sites
        # always supply the author as requester; missing DM provenance fails
        # closed.
        reject_for_permissions = False
        if guild_scope == "@me":
            reject_for_permissions = requester is None or not _requester_can_read(
                channel, requester
            )
        elif not caller_is_admin and requester is not None:
            reject_for_permissions = not _requester_can_read(channel, requester)
        if reject_for_permissions:
            log.warning(
                "Rejecting message-link resolution: user=%s lacks read access to channel=%s",
                getattr(requester, "id", "?"),
                channel_id,
            )
            return match, None

        msg = await channel.fetch_message(message_id)

        parts: list[str] = [
            f"[Referenced message by {msg.author.display_name} in #{getattr(channel, 'name', channel_id)}]"
        ]
        if msg.content:
            parts.append(msg.content)
        for embed in msg.embeds:
            formatted = format_embed(embed)
            if formatted:
                parts.append(formatted)
        # Download text attachments inline (e.g. message.txt from long Discord messages)
        for attachment in msg.attachments:
            content_type = (attachment.content_type or "").split(";")[0].strip().lower()
            if (
                content_type in _INLINE_TEXT_TYPES
                and (attachment.size or 0) <= _MAX_LINK_TEXT_BYTES
            ):
                # SSRF guard mirrored from utils/attachments._host_is_allowed.
                # Discord occasionally serves attachments from unexpected
                # hosts (or a crafted message-link target could be spoofed
                # via CDN redirect quirks); refuse anything that isn't a
                # Discord CDN https URL before issuing the request.
                attachment_url = attachment.url or ""
                allowed = False
                try:
                    parsed = urlparse(attachment_url)
                    if (
                        parsed.scheme == "https"
                        and (parsed.hostname or "").lower() in ALLOWED_ATTACHMENT_HOSTS
                    ):
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
                                                parts.append(
                                                    f"[Attachment: {attachment.filename}]"
                                                )
                                                continue
                                        except ValueError:
                                            pass
                                    data = b""
                                    async for chunk in resp.content.iter_chunked(
                                        64 * 1024
                                    ):
                                        data += chunk
                                        if len(data) > _MAX_LINK_TEXT_BYTES:
                                            data = b""
                                            break
                                    if 0 < len(data) <= _MAX_LINK_TEXT_BYTES:
                                        try:
                                            text = data.decode("utf-8")
                                        except UnicodeDecodeError:
                                            text = data.decode("latin-1")
                                        parts.append(
                                            f"[Attachment: {attachment.filename}]\n{text}"
                                        )
                                        continue
                    except Exception as e:
                        log.warning(
                            "Failed to download linked attachment %s: %s",
                            attachment.filename,
                            e,
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
                    if requester is not None and not _requester_can_read(
                        channel, requester
                    ):
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
                    if requester is not None and not _requester_can_read(
                        channel, requester
                    ):
                        continue
                    body = _format_msg_body(m)
                    if body:
                        after_msgs.append((m.author.display_name, body))

                all_ctx = (
                    before_msgs
                    + [(msg.author.display_name, _format_msg_body(msg))]
                    + after_msgs
                )
                ctx_lines, _ = _build_layered_lines(all_ctx, oldest_first=True)
                if ctx_lines:
                    parts.append(
                        f"\n[Thread context from #{getattr(channel, 'name', channel_id)} "
                        f"({len(all_ctx)} messages around referenced)]\n"
                        + "\n".join(ctx_lines)
                        + "\n[End of cross-thread context]"
                    )
                log.info(
                    "Cross-thread context fetched: thread=%s msgs=%d",
                    channel.name,
                    len(all_ctx),
                )
            except Exception as e:
                log.warning(
                    "Failed to fetch cross-thread context for %s: %s", channel_id, e
                )

        log.info(
            "Resolved message link: guild=%s ch=%s msg=%s",
            guild_scope,
            channel_id,
            message_id,
        )
        return match, "\n".join(parts)
    except discord.NotFound as e:
        log.info("Message link target not found %s: %s", match.group(0), e)
        return match, None
    except discord.Forbidden as e:
        log.warning("Message link target forbidden %s: %s", match.group(0), e)
        return match, None
    except (discord.HTTPException, discord.InvalidData) as e:
        log.warning(
            "Message link API failure %s (%s): %s",
            match.group(0),
            type(e).__name__,
            e,
        )
        return match, None
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
    matches = list(DISCORD_MSG_URL_RE.finditer(text))
    if not matches:
        return text
    return await context_gateway.resolve_message_links(
        discord_client,
        text,
        requester=requester,
        caller_is_admin=caller_is_admin,
        matches=matches,
        max_links=MAX_RESOLVED_LINKS,
        resolve_one=_resolve_one_link,
    )


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
    bot_user_id: int | None = None,
    admin_user_ids: Collection[int] = ADMIN_USER_IDS,
) -> str:
    """Detect <#channel_id> mentions that point to threads and fetch their context.

    Returns a formatted string of cross-thread contexts (empty string if none).
    Skips the current channel/thread to avoid duplication.
    """
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

    return await context_gateway.resolve_channel_mention_contexts(
        discord_client,
        channel_ids,
        requester=requester,
        caller_is_admin=caller_is_admin,
        bot_user_id=bot_user_id,
        admin_user_ids=admin_user_ids,
        context_messages=CROSS_THREAD_CONTEXT_MESSAGES,
        max_chars=MAX_CROSS_THREAD_CHARS,
        requester_can_read=_requester_can_read,
        should_include=_should_include_msg,
        format_body=_format_msg_body,
        build_lines=_build_layered_lines,
    )


class ReplyChainSender:
    """Send a sequence of messages where the first replies to the triggering
    message and the rest are plain channel sends.

    Collapses the hand-rolled ``is_first`` flag that bot.py repeated at every
    send site (text / table / math segments, final reply chains) into one
    place (P2-b of docs/2026-07-03-grand-refactor-plan.md).
    """

    def __init__(self, message: "discord.Message") -> None:
        self._message = message
        self._first = True

    async def send(self, content: "str | None" = None, **kwargs) -> "discord.Message":
        if self._first:
            self._first = False
            if content is None:
                return await self._message.reply(**kwargs)
            return await self._message.reply(content, **kwargs)
        channel = self._message.channel
        if content is None:
            return await channel.send(**kwargs)
        return await channel.send(content, **kwargs)

    async def send_split(self, text: str) -> None:
        """Split ``text`` to fit Discord's 2000-char limit and send each chunk."""
        for chunk in split_message(text):
            await self.send(chunk)

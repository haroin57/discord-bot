"""Discord message processing utilities — matches free-code utils/messages.ts pattern."""

import re
import logging

import discord

from config import BOT_KEYWORDS, BOT_ROLE_IDS, MAX_RESOLVED_LINKS, THREAD_HISTORY_LIMIT

log = logging.getLogger(__name__)

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


async def fetch_thread_context(thread: discord.Thread, before_message: discord.Message) -> str:
    """Fetch past messages in a thread and format them inline (capped at MAX_CONTEXT_CHARS)."""
    lines: list[str] = []
    count = 0
    total_chars = 0
    async for msg in thread.history(limit=THREAD_HISTORY_LIMIT, before=before_message, oldest_first=True):
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
        return ""
    return f"[Thread: {thread.name} - past {count} messages]\n" + "\n".join(lines) + "\n[End of thread history]"


CHANNEL_CONTEXT_LIMIT = 10


async def fetch_channel_context(channel, before_message: discord.Message) -> str:
    """Fetch recent messages in a regular channel (capped at MAX_CONTEXT_CHARS)."""
    lines: list[str] = []
    total_chars = 0
    count = 0
    async for msg in channel.history(limit=CHANNEL_CONTEXT_LIMIT, before=before_message):
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
        return ""
    lines.reverse()
    return f"[Channel recent {count} messages]\n" + "\n".join(lines) + "\n[End of channel history]"


# ---------------------------------------------------------------------------
# Message link resolution
# ---------------------------------------------------------------------------
DISCORD_MSG_URL_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


async def _resolve_one_link(discord_client: discord.Client, match: re.Match) -> tuple[re.Match, str | None]:
    guild_id, channel_id, message_id = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        channel = discord_client.get_channel(channel_id) or await discord_client.fetch_channel(channel_id)
        msg = await channel.fetch_message(message_id)

        parts: list[str] = [f"[Referenced message by {msg.author.display_name} in #{channel.name}]"]
        if msg.content:
            parts.append(msg.content)
        for embed in msg.embeds:
            formatted = format_embed(embed)
            if formatted:
                parts.append(formatted)
        for attachment in msg.attachments:
            parts.append(f"[Attachment: {attachment.filename} ({attachment.url})]")

        log.info("Resolved message link: guild=%s ch=%s msg=%s", guild_id, channel_id, message_id)
        return match, "\n".join(parts)
    except Exception as e:
        log.warning("Failed to resolve message link %s: %s", match.group(0), e)
        return match, None


async def resolve_message_links(discord_client: discord.Client, text: str) -> str:
    """Find Discord message URLs in text, fetch content in parallel, and inline them."""
    import asyncio
    matches = list(DISCORD_MSG_URL_RE.finditer(text))
    if not matches:
        return text

    matches = matches[:MAX_RESOLVED_LINKS]
    results = await asyncio.gather(*[_resolve_one_link(discord_client, m) for m in matches])
    for match, replacement in reversed(results):
        if replacement is not None:
            text = text[:match.start()] + replacement + text[match.end():]
    return text

"""Discord context I/O gateway with bounded, owned fan-out."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any

import discord

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One context line with identity and boundary trust preserved."""

    author_id: int | None
    author_name: str
    body: str
    rendered: str
    trusted: bool


class ContextBlock(str):
    """Prompt-ready context string carrying per-message provenance."""

    items: tuple[ContextItem, ...]
    label: str

    def __new__(
        cls,
        value: str,
        *,
        items: tuple[ContextItem, ...],
        label: str,
    ) -> "ContextBlock":
        instance = super().__new__(cls, value)
        instance.items = items
        instance.label = label
        return instance


class ResolvedMessageLinks(str):
    """Rendered compatibility text plus provenance-safe link context.

    String callers continue to see the historical URL-replaced rendering.
    Prompt assembly uses ``original_text`` as the caller-authored instruction
    and places ``linked_contexts`` in a separate untrusted block.
    """

    original_text: str
    linked_contexts: tuple[str, ...]

    def __new__(
        cls,
        value: str,
        *,
        original_text: str,
        linked_contexts: tuple[str, ...],
    ) -> "ResolvedMessageLinks":
        instance = super().__new__(cls, value)
        instance.original_text = original_text
        instance.linked_contexts = linked_contexts
        return instance


def _context_item(
    message: discord.Message,
    body: str,
    *,
    caller_is_admin: bool,
    bot_user_id: int | None,
    admin_user_ids: Collection[int],
    build_lines: Callable[[list[tuple[str, str]]], tuple[list[str], int]],
) -> ContextItem:
    author = message.author
    name = getattr(author, "display_name", "Unknown")
    author_id = getattr(author, "id", None)
    # Discord's author.bot flag is attacker-independent provenance, but it is
    # not an authorization boundary: webhooks and unrelated bots also set it.
    # Only identities injected by the composition root may cross the trusted
    # prompt boundary. A missing current-bot identity therefore fails closed.
    known_bot_id = (
        bot_user_id
        if isinstance(bot_user_id, int) and not isinstance(bot_user_id, bool)
        else None
    )
    trusted = bool(
        caller_is_admin
        and isinstance(author_id, int)
        and not isinstance(author_id, bool)
        and (author_id == known_bot_id or author_id in admin_user_ids)
    )
    lines, _ = build_lines([(name, body)])
    rendered = lines[0] if lines else f"{name}: {body}"
    return ContextItem(author_id, name, body, rendered, trusted)


def _bounded_items(
    items: list[ContextItem], max_context_chars: int
) -> tuple[list[ContextItem], bool]:
    selected: list[ContextItem] = []
    total_chars = 0
    for item in items:
        rendered_size = len(item.rendered)
        if total_chars + rendered_size > max_context_chars:
            return selected, True
        selected.append(item)
        total_chars += rendered_size
    return selected, False


def render_context_items(
    block: ContextBlock,
    *,
    trusted: bool,
) -> str:
    """Render one trust partition without discarding its original label."""
    lines = [item.rendered for item in block.items if item.trusted is trusted]
    if not lines:
        return ""
    return (
        f"[{block.label}: {len(lines)} messages]\n"
        + "\n".join(lines)
        + f"\n[End of {block.label}]"
    )


async def _gather_owned(awaitables: list[Awaitable[Any]]) -> list[Any]:
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    if not tasks:
        return []
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def fetch_thread_context(
    thread: discord.Thread,
    before_message: discord.Message,
    *,
    caller_is_admin: bool,
    bot_user_id: int | None,
    admin_user_ids: Collection[int],
    layer1_count: int,
    layer2_count: int,
    cache_get: Callable[[tuple[int, int, bool]], str | None],
    cache_set: Callable[[tuple[int, int, bool], str], None],
    should_include: Callable[[discord.Message, bool], bool],
    format_body: Callable[[discord.Message], str],
    build_lines: Callable[[list[tuple[str, str]]], tuple[list[str], int]],
    max_context_chars: int = 20_000,
) -> str:
    cache_key = (thread.id, before_message.id, bool(caller_is_admin))
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    items: list[ContextItem] = []
    async for message in thread.history(
        limit=layer1_count + layer2_count,
        before=before_message,
        oldest_first=True,
    ):
        if not should_include(message, caller_is_admin):
            continue
        body = format_body(message)
        if body:
            items.append(
                _context_item(
                    message,
                    body,
                    caller_is_admin=caller_is_admin,
                    bot_user_id=bot_user_id,
                    admin_user_ids=admin_user_ids,
                    build_lines=build_lines,
                )
            )

    if not items:
        cache_set(cache_key, "")
        return ""
    items, truncated = _bounded_items(items, max_context_chars)
    lines, _ = build_lines([(item.author_name, item.body) for item in items])
    if truncated and "(...truncated...)" not in lines:
        lines.append("(...truncated...)")
    result = (
        f"[Thread: {thread.name} - past {len(items)} messages]\n"
        + "\n".join(lines)
        + "\n[End of thread history]"
    )
    block = ContextBlock(
        result,
        items=tuple(items),
        label=f"Thread {thread.name}",
    )
    cache_set(cache_key, block)
    return block


async def fetch_channel_context(
    channel,
    before_message: discord.Message,
    *,
    caller_is_admin: bool,
    bot_user_id: int | None,
    admin_user_ids: Collection[int],
    context_limit: int,
    cache_get: Callable[[tuple[int, int, bool]], str | None],
    cache_set: Callable[[tuple[int, int, bool], str], None],
    should_include: Callable[[discord.Message, bool], bool],
    format_body: Callable[[discord.Message], str],
    build_lines: Callable[[list[tuple[str, str]]], tuple[list[str], int]],
    max_context_chars: int = 20_000,
) -> str:
    cache_key = (channel.id, before_message.id, bool(caller_is_admin))
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    items: list[ContextItem] = []
    async for message in channel.history(limit=context_limit, before=before_message):
        if not should_include(message, caller_is_admin):
            continue
        body = format_body(message)
        if body:
            items.append(
                _context_item(
                    message,
                    body,
                    caller_is_admin=caller_is_admin,
                    bot_user_id=bot_user_id,
                    admin_user_ids=admin_user_ids,
                    build_lines=build_lines,
                )
            )

    if not items:
        cache_set(cache_key, "")
        return ""
    items.reverse()
    items, truncated = _bounded_items(items, max_context_chars)
    lines, _ = build_lines([(item.author_name, item.body) for item in items])
    if truncated and "(...truncated...)" not in lines:
        lines.append("(...truncated...)")
    result = (
        f"[Channel recent {len(items)} messages]\n"
        + "\n".join(lines)
        + "\n[End of channel history]"
    )
    block = ContextBlock(
        result,
        items=tuple(items),
        label="Channel history",
    )
    cache_set(cache_key, block)
    return block


async def resolve_message_links(
    discord_client: discord.Client,
    text: str,
    *,
    requester,
    caller_is_admin: bool,
    matches: list,
    max_links: int,
    resolve_one: Callable[..., Awaitable[tuple[Any, str | None]]],
) -> str:
    original_text = text
    selected = matches[:max_links]
    results = await _gather_owned(
        [
            resolve_one(
                discord_client,
                match,
                requester,
                caller_is_admin=caller_is_admin,
            )
            for match in selected
        ]
    )
    for match, replacement in reversed(results):
        if replacement is not None:
            text = text[: match.start()] + replacement + text[match.end() :]
    linked_contexts = tuple(
        replacement for _match, replacement in results if replacement is not None
    )
    if not linked_contexts:
        return text
    return ResolvedMessageLinks(
        text,
        original_text=original_text,
        linked_contexts=linked_contexts,
    )


async def resolve_channel_mention_contexts(
    discord_client: discord.Client,
    channel_ids: list[int],
    *,
    requester,
    caller_is_admin: bool,
    bot_user_id: int | None,
    admin_user_ids: Collection[int],
    context_messages: int,
    max_chars: int,
    requester_can_read: Callable[[object, object], bool],
    should_include: Callable[[discord.Message, bool], bool],
    format_body: Callable[[discord.Message], str],
    build_lines: Callable[[list[tuple[str, str]]], tuple[list[str], int]],
) -> str:
    async def fetch_one(channel_id: int) -> str | None:
        try:
            channel = discord_client.get_channel(
                channel_id
            ) or await discord_client.fetch_channel(channel_id)
            if channel is None or not isinstance(channel, discord.Thread):
                return None
            if requester is not None and not requester_can_read(channel, requester):
                log.warning(
                    "Rejecting channel-mention context: user=%s lacks access to thread=%s",
                    getattr(requester, "id", "?"),
                    channel_id,
                )
                return None

            items: list[ContextItem] = []
            async for message in channel.history(
                limit=context_messages * 2,
                oldest_first=False,
            ):
                if not should_include(message, caller_is_admin):
                    continue
                body = format_body(message)
                if body:
                    items.append(
                        _context_item(
                            message,
                            body,
                            caller_is_admin=caller_is_admin,
                            bot_user_id=bot_user_id,
                            admin_user_ids=admin_user_ids,
                            build_lines=build_lines,
                        )
                    )
            if not items:
                return None

            items.reverse()
            lines = [item.rendered for item in items]
            result = (
                f"[Cross-thread context: #{channel.name} ({len(items)} recent messages)]\n"
                + "\n".join(lines)
                + "\n[End of cross-thread context]"
            )
            if len(result) > max_chars:
                result = result[:max_chars] + "\n[...truncated...]"
            log.info(
                "Channel-mention context fetched: thread=%s msgs=%d",
                channel.name,
                len(items),
            )
            return ContextBlock(
                result,
                items=tuple(items),
                label=f"Cross-thread #{channel.name}",
            )
        except Exception as exc:
            log.warning(
                "Failed to fetch channel-mention context for %s: %s",
                channel_id,
                exc,
            )
            return None

    results = await _gather_owned([fetch_one(channel_id) for channel_id in channel_ids])
    blocks = [result for result in results if result]
    if not blocks:
        return ""
    items = tuple(
        item
        for block in blocks
        if isinstance(block, ContextBlock)
        for item in block.items
    )
    return ContextBlock(
        "\n\n".join(str(result) for result in blocks),
        items=items,
        label="Cross-thread context",
    )

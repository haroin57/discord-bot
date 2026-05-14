"""Unit tests for services.vscode_sync.discord_post.make_post_callback."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from services.vscode_sync.discord_post import make_post_callback


def _make_channel():
    channel = MagicMock()
    channel.send = AsyncMock()
    return channel


async def test_cached_channel_send():
    channel = _make_channel()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    post = make_post_callback(bot, 12345)

    await post("hi")
    channel.send.assert_awaited_once()
    args, kwargs = channel.send.call_args
    assert args[0] == "hi"
    assert kwargs["allowed_mentions"].everyone is False


async def test_fetched_channel_when_not_cached():
    channel = _make_channel()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=channel)

    post = make_post_callback(bot, 12345)
    await post("hi")
    bot.fetch_channel.assert_awaited_once_with(12345)
    channel.send.assert_awaited_once()


async def test_channel_cached_across_calls():
    channel = _make_channel()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=channel)

    post = make_post_callback(bot, 12345)
    await post("first")
    await post("second")
    await post("third")

    assert bot.fetch_channel.await_count == 1
    assert channel.send.await_count == 3


async def test_thread_not_found_swallowed():
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))

    post = make_post_callback(bot, 999)
    await post("hi")


async def test_forbidden_resolve_swallowed():
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "x"))

    post = make_post_callback(bot, 999)
    await post("hi")


async def test_send_forbidden_invalidates_cache():
    channel = _make_channel()
    channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "x"))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    post = make_post_callback(bot, 12345)
    await post("hi")
    assert bot.get_channel.call_count == 1

    channel.send = AsyncMock()
    await post("hi2")
    assert bot.get_channel.call_count == 2
    channel.send.assert_awaited_once()


async def test_send_http_exception_swallowed():
    channel = _make_channel()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    post = make_post_callback(bot, 12345)
    await post("hi")


async def test_empty_content_noop():
    channel = _make_channel()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    post = make_post_callback(bot, 12345)

    await post("")
    channel.send.assert_not_awaited()
    bot.get_channel.assert_not_called()


async def test_content_truncated_to_2000():
    channel = _make_channel()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    post = make_post_callback(bot, 12345)

    await post("x" * 3000)
    args, _ = channel.send.call_args
    assert len(args[0]) == 2000


async def test_allowed_mentions_none():
    channel = _make_channel()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    post = make_post_callback(bot, 12345)

    await post("@everyone hi")
    _, kwargs = channel.send.call_args
    am = kwargs["allowed_mentions"]
    assert am.everyone is False
    assert am.users is False
    assert am.roles is False


async def test_suppress_embeds_set():
    channel = _make_channel()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    post = make_post_callback(bot, 12345)

    await post("https://example.com")
    _, kwargs = channel.send.call_args
    assert kwargs.get("suppress_embeds") is True

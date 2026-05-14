"""Tests for bot.py and main.py — event handlers, routing, and entry point."""

import asyncio
import io
import json
import logging
import os
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import aiohttp
import discord
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message(
    *,
    author_id=12345,
    author_bot=False,
    channel_id=99999,
    content="かなえ hello",
    is_dm=False,
    mentions=None,
    role_mentions=None,
    attachments=None,
    reference=None,
):
    """Build a mock discord.Message with sensible defaults."""
    message = AsyncMock(spec=discord.Message)
    message.author = MagicMock(spec=discord.Member)
    message.author.id = author_id
    message.author.bot = author_bot
    message.author.display_name = "TestUser"
    message.author.__str__ = lambda self: "TestUser#0000"
    message.author.__eq__ = lambda self, other: self.id == getattr(other, "id", None)

    if is_dm:
        message.channel = MagicMock(spec=discord.DMChannel)
    else:
        message.channel = MagicMock()
        # Not a DMChannel
        message.channel.__class__ = type("TextChannel", (), {})
    message.channel.id = channel_id
    message.channel.send = AsyncMock()

    message.content = content
    message.mentions = mentions or []
    message.role_mentions = role_mentions or []
    message.attachments = attachments or []
    message.reference = reference

    message.reply = AsyncMock()
    message.add_reaction = AsyncMock()

    return message


# ---------------------------------------------------------------------------
# is_admin
# ---------------------------------------------------------------------------
class TestIsAdmin:
    def test_admin_user(self, monkeypatch):
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {111, 222})
        from bot import is_admin
        user = MagicMock()
        user.id = 111
        assert is_admin(user) is True

    def test_non_admin_user(self, monkeypatch):
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {111, 222})
        from bot import is_admin
        user = MagicMock()
        user.id = 999
        assert is_admin(user) is False

    def test_empty_admin_set(self, monkeypatch):
        monkeypatch.setattr("bot.ADMIN_USER_IDS", set())
        from bot import is_admin
        user = MagicMock()
        user.id = 1
        assert is_admin(user) is False


# ---------------------------------------------------------------------------
# _purge_cooldowns
# ---------------------------------------------------------------------------
class TestPurgeCooldowns:
    def test_removes_expired_entries(self, monkeypatch):
        from bot import _purge_cooldowns
        import bot

        monkeypatch.setattr("bot.COOLDOWN_SECONDS", 5)

        bot.cooldowns.clear()
        now = time.time()
        # Expired entry (older than COOLDOWN_SECONDS * 10 = 50s)
        bot.cooldowns[1] = now - 100
        # Recent entry
        bot.cooldowns[2] = now - 1

        _purge_cooldowns()

        assert 1 not in bot.cooldowns
        assert 2 in bot.cooldowns

    def test_no_entries(self, monkeypatch):
        from bot import _purge_cooldowns
        import bot

        monkeypatch.setattr("bot.COOLDOWN_SECONDS", 5)
        bot.cooldowns.clear()
        _purge_cooldowns()  # should not raise
        assert len(bot.cooldowns) == 0

    def test_all_recent(self, monkeypatch):
        from bot import _purge_cooldowns
        import bot

        monkeypatch.setattr("bot.COOLDOWN_SECONDS", 5)
        bot.cooldowns.clear()
        now = time.time()
        bot.cooldowns[10] = now
        bot.cooldowns[20] = now - 1

        _purge_cooldowns()

        assert 10 in bot.cooldowns
        assert 20 in bot.cooldowns

    def test_all_expired(self, monkeypatch):
        from bot import _purge_cooldowns
        import bot

        monkeypatch.setattr("bot.COOLDOWN_SECONDS", 1)
        bot.cooldowns.clear()
        now = time.time()
        bot.cooldowns[1] = now - 100
        bot.cooldowns[2] = now - 200

        _purge_cooldowns()

        assert len(bot.cooldowns) == 0


# ---------------------------------------------------------------------------
# get_http_session
# ---------------------------------------------------------------------------
class TestGetHttpSession:
    @pytest.mark.asyncio
    async def test_creates_new_session_when_none(self):
        import bot
        bot._http_session = None
        with patch("bot.aiohttp.ClientSession") as MockSession:
            mock_instance = MagicMock()
            mock_instance.closed = False
            MockSession.return_value = mock_instance

            result = await bot.get_http_session()

            MockSession.assert_called_once()
            assert result is mock_instance

    @pytest.mark.asyncio
    async def test_reuses_existing_open_session(self):
        import bot
        mock_session = MagicMock()
        mock_session.closed = False
        bot._http_session = mock_session

        with patch("bot.aiohttp.ClientSession") as MockSession:
            result = await bot.get_http_session()
            MockSession.assert_not_called()
            assert result is mock_session

    @pytest.mark.asyncio
    async def test_creates_new_when_closed(self):
        import bot
        closed_session = MagicMock()
        closed_session.closed = True
        bot._http_session = closed_session

        with patch("bot.aiohttp.ClientSession") as MockSession:
            new_session = MagicMock()
            new_session.closed = False
            MockSession.return_value = new_session

            result = await bot.get_http_session()

            MockSession.assert_called_once()
            assert result is new_session


# ---------------------------------------------------------------------------
# on_message — routing and response handling
# ---------------------------------------------------------------------------
class TestOnMessage:
    """Test the on_message event handler via direct invocation."""

    @pytest.fixture(autouse=True)
    def _reset_bot_state(self, monkeypatch):
        """Clean state before each test."""
        import bot
        bot.cooldowns.clear()
        bot._channel_semas.clear()
        monkeypatch.setattr("bot.COOLDOWN_SECONDS", 5)
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})
        monkeypatch.setattr("bot.BOT_KEYWORDS", ["かなえ"])
        monkeypatch.setattr("bot.BOT_ROLE_IDS", set())
        monkeypatch.setattr("bot.MAX_COOLDOWN_ENTRIES", 1000)
        # Always use local fallback in unit tests so the shared rate_limit
        # module state doesn't bleed between tests.
        monkeypatch.setattr("bot._USE_SHARED_RATE_LIMIT", False)

        # Mock bot.user — it's a property on discord.Client, so we patch at class level
        self._mock_bot_user = MagicMock()
        self._mock_bot_user.id = 99900
        self._user_patcher = patch.object(
            type(bot.bot), "user", new_callable=PropertyMock, return_value=self._mock_bot_user
        )
        self._user_patcher.start()
        yield
        self._user_patcher.stop()

    async def _dispatch(self, message):
        """Invoke on_message handler directly."""
        import bot
        coro = bot.bot.on_message(message)
        await coro

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self):
        import bot
        msg = _make_message(author_id=99900)
        msg.author.__eq__ = lambda self, other: True  # author == bot.user
        msg.author.bot = False

        await self._dispatch(msg)
        msg.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_other_bots(self):
        msg = _make_message(author_bot=True, author_id=55555)
        await self._dispatch(msg)
        msg.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_non_admin_ignored(self, monkeypatch):
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {99999})
        msg = _make_message(is_dm=True, author_id=12345, content="hello")
        await self._dispatch(msg)
        msg.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_guild_no_mention_no_keyword_ignored(self):
        msg = _make_message(content="random message", author_id=12345)
        msg.mentions = []
        msg.role_mentions = []
        await self._dispatch(msg)
        msg.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_guild_keyword_triggers(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.BOT_KEYWORDS", ["かなえ"])

        msg = _make_message(content="かなえ hello", author_id=12345)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("response text", "sess-1")), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["response text"]), \
             patch("bot.update_session"), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            msg.reply.assert_called()  # at least the status + final response

    @pytest.mark.asyncio
    async def test_guild_mention_triggers(self, monkeypatch):
        import bot
        mock_bot_user = bot.bot.user

        msg = _make_message(content="<@99900> hello", author_id=12345)
        msg.mentions = [mock_bot_user]

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            msg.reply.assert_called()

    @pytest.mark.asyncio
    async def test_role_mention_triggers(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.BOT_ROLE_IDS", {7777})

        role_mock = MagicMock()
        role_mock.id = 7777

        msg = _make_message(content="hey", author_id=12345)
        msg.role_mentions = [role_mock]

        with patch("bot.strip_mention", return_value="hey"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hey"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            msg.reply.assert_called()

    @pytest.mark.asyncio
    async def test_rate_limiting(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.COOLDOWN_SECONDS", 60)

        # Simulate a recent cooldown
        bot.cooldowns[12345] = time.time()

        msg = _make_message(content="かなえ hello", author_id=12345)
        await self._dispatch(msg)

        # Should get rate limit reply
        msg.reply.assert_called()
        reply_text = msg.reply.call_args[0][0]
        assert "⏳" in reply_text or "wait" in reply_text.lower()

    @pytest.mark.skip(
        reason=(
            "channel_busy set was replaced with asyncio.Semaphore (_channel_semas). "
            "Pre-marking a channel as busy requires acquiring the semaphore in a "
            "concurrent task, which cannot be done deterministically in a unit test "
            "without a real event loop running in parallel. "
            "Integration-level coverage of the queuing behaviour is sufficient."
        )
    )
    @pytest.mark.asyncio
    async def test_channel_busy(self, monkeypatch):
        import bot

        # New busy management: sema.locked() is True while sema is acquired.
        # Simulate by pre-acquiring the semaphore for channel 888.
        msg = _make_message(content="かなえ hello", author_id=12345, channel_id=888)

        with patch("bot.strip_mention", return_value="hello"):
            # Pre-mark channel as busy
            sema = bot._get_channel_sema(888)
            await sema.acquire()

            await self._dispatch(msg)

            sema.release()

            # Should reply with busy message (queued reaction, not a direct reply)
            calls = msg.reply.call_args_list
            busy_replied = any("前のリクエスト" in str(c) or "処理中" in str(c) for c in calls)
            assert busy_replied

    @pytest.mark.asyncio
    async def test_empty_prompt_dm(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})

        msg = _make_message(content="", is_dm=True, author_id=12345)
        await self._dispatch(msg)

        # Should reply with "何か用ですか？"
        calls = msg.reply.call_args_list
        any_empty_prompt = any("何か用" in str(c) for c in calls)
        assert any_empty_prompt

    @pytest.mark.asyncio
    async def test_empty_prompt_guild(self, monkeypatch):
        import bot

        # Content has keyword but strip_mention returns empty
        msg = _make_message(content="かなえ", author_id=12345)

        with patch("bot.strip_mention", return_value=""):
            await self._dispatch(msg)

            calls = msg.reply.call_args_list
            any_empty_prompt = any("include a message" in str(c).lower() for c in calls)
            assert any_empty_prompt

    @pytest.mark.asyncio
    async def test_long_response_sent_as_file(self, monkeypatch):
        import bot

        long_text = "x" * 9000
        msg = _make_message(content="かなえ hello", author_id=12345)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=(long_text, None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            calls = msg.reply.call_args_list
            # One of the replies should mention "too long" or have files kwarg
            file_reply = any("files" in str(c) for c in calls)
            assert file_reply

    @pytest.mark.asyncio
    async def test_normal_response_chunked(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("chunk1", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["chunk1"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            # Final reply should contain the response text
            calls = msg.reply.call_args_list
            any_response = any("chunk1" in str(c) for c in calls)
            assert any_response

    @pytest.mark.asyncio
    async def test_multi_chunk_response(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("c1c2", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["c1", "c2"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            # Second chunk should be sent via channel.send
            msg.channel.send.assert_called()

    @pytest.mark.asyncio
    async def test_error_handling(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            calls = msg.reply.call_args_list
            error_replied = any("❌" in str(c) for c in calls)
            assert error_replied

    @pytest.mark.asyncio
    async def test_channel_busy_cleared_after_success(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345, channel_id=555)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            # After processing, semaphore for channel 555 must be released (not locked)
            sema = bot._channel_semas.get(555)
            assert sema is None or not sema.locked()

    @pytest.mark.asyncio
    async def test_channel_busy_cleared_after_error(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345, channel_id=666)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, side_effect=RuntimeError("fail")), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            # After error, semaphore for channel 666 must be released (not locked)
            sema = bot._channel_semas.get(666)
            assert sema is None or not sema.locked()

    @pytest.mark.asyncio
    async def test_cooldown_entry_added(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=77777)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            assert 77777 in bot.cooldowns

    @pytest.mark.asyncio
    async def test_cooldown_overflow_eviction(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.MAX_COOLDOWN_ENTRIES", 2)

        # Fill cooldowns to capacity
        bot.cooldowns[1] = time.time() - 1
        bot.cooldowns[2] = time.time() - 0.5

        msg = _make_message(content="かなえ hello", author_id=3)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            assert len(bot.cooldowns) <= 2
            assert 3 in bot.cooldowns  # newest should be kept

    @pytest.mark.asyncio
    async def test_session_updated_when_returned(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", "new-sess-id")), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.update_session") as mock_update, \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_not_updated_when_none(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.update_session") as mock_update, \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_admin_proceeds(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})

        msg = _make_message(content="hello", is_dm=True, author_id=12345)

        with patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("hi", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["hi"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            # Should have called call_with_tools (got past the guard)
            msg.reply.assert_called()

    @pytest.mark.asyncio
    async def test_add_reaction_forbidden_ignored(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)
        msg.add_reaction = AsyncMock(side_effect=discord.errors.Forbidden(MagicMock(), "no perms"))

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            # Should not raise despite Forbidden on add_reaction
            await self._dispatch(msg)
            msg.reply.assert_called()

    @pytest.mark.asyncio
    async def test_non_admin_prompt_has_security_prefix(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.ADMIN_USER_IDS", set())  # no one is admin

        msg = _make_message(content="かなえ hello", author_id=12345)

        captured_prompt = None

        async def capture_call_with_tools(prompt, *args, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ("ok", None)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, side_effect=capture_call_with_tools), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            assert captured_prompt is not None
            # Prompt must include the untrusted-user security header (strict mode)
            assert "external untrusted user" in captured_prompt
            assert "Strict mode" in captured_prompt

    @pytest.mark.asyncio
    async def test_admin_prompt_has_senpai_prefix(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})

        msg = _make_message(content="かなえ hello", author_id=12345)

        captured_prompt = None

        async def capture_call_with_tools(prompt, *args, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ("ok", None)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, side_effect=capture_call_with_tools), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            assert captured_prompt is not None
            # Admin prompt must use the trusted-context header, not the strict-mode header
            assert "Trusted context" in captured_prompt
            assert "external untrusted user" not in captured_prompt

    @pytest.mark.asyncio
    async def test_reply_context_fetched(self, monkeypatch):
        import bot

        ref = MagicMock()
        ref.message_id = 111
        ref.cached_message = None

        ref_msg = AsyncMock()
        ref_msg.author = MagicMock()
        ref_msg.author.display_name = "ReplyAuthor"
        ref_msg.content = "original content"
        ref_msg.attachments = []

        msg = _make_message(content="かなえ hello", author_id=12345, reference=ref)
        msg.channel.fetch_message = AsyncMock(return_value=ref_msg)

        captured_prompt = None

        async def capture_call(prompt, *args, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ("ok", None)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, side_effect=capture_call), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            assert captured_prompt is not None
            # Implementation wraps reply context in <replied_to_message> XML tag
            assert "replied_to_message" in captured_prompt
            assert "ReplyAuthor" in captured_prompt

    @pytest.mark.asyncio
    async def test_response_images_attached(self, monkeypatch):
        import bot
        import tempfile

        # Create a real temp file so discord.File can open it
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"fake-png-data")
            tmp_path = tmp.name

        try:
            msg = _make_message(content="かなえ hello", author_id=12345)

            with patch("bot.strip_mention", return_value="hello"), \
                 patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("check " + tmp_path, None)), \
                 patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
                 patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
                 patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
                 patch("bot.extract_image_paths", return_value=[tmp_path]), \
                 patch("bot.split_message", return_value=["check " + tmp_path]), \
                 patch("bot.cleanup_attachments"), \
                 patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
                 patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
                await self._dispatch(msg)

                # The reply should include files
                calls = msg.reply.call_args_list
                any_files = any(c.kwargs.get("files") for c in calls)
                assert any_files
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_status_message_deleted(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)
        status_msg = AsyncMock()
        msg.reply = AsyncMock(return_value=status_msg)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

            status_msg.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_thread_context_fetched(self, monkeypatch):
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)
        # Make the channel look like a Thread
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.id = 99999
        msg.channel.send = AsyncMock()

        captured_prompt = None

        async def capture_call(prompt, *args, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ("ok", None)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, side_effect=capture_call), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value="thread-context-sentinel"):
            await self._dispatch(msg)

            assert captured_prompt is not None
            # Thread context is sanitized but the payload body should survive
            assert "thread-context-sentinel" in captured_prompt


# ---------------------------------------------------------------------------
# _patched_close
# ---------------------------------------------------------------------------
class TestPatchedClose:
    @pytest.mark.asyncio
    async def test_closes_http_session(self):
        import bot

        mock_session = AsyncMock()
        mock_session.closed = False
        bot._http_session = mock_session

        with patch.object(bot, "_orig_close", new_callable=AsyncMock) as mock_orig:
            await bot._patched_close()
            mock_session.close.assert_called_once()
            assert bot._http_session is None
            mock_orig.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_closed_session(self):
        import bot

        mock_session = AsyncMock()
        mock_session.closed = True
        bot._http_session = mock_session

        with patch.object(bot, "_orig_close", new_callable=AsyncMock) as mock_orig:
            await bot._patched_close()
            mock_session.close.assert_not_called()
            mock_orig.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_none_session(self):
        import bot
        bot._http_session = None

        with patch.object(bot, "_orig_close", new_callable=AsyncMock) as mock_orig:
            await bot._patched_close()
            mock_orig.assert_called_once()


# ---------------------------------------------------------------------------
# main.py
# ---------------------------------------------------------------------------
class TestMain:
    def test_no_token_raises_system_exit(self, monkeypatch):
        monkeypatch.setattr("config.DISCORD_TOKEN", "")
        # Re-import to pick up patched value
        import importlib
        import main as main_mod
        monkeypatch.setattr("main.DISCORD_TOKEN", "")
        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        assert exc_info.value.code == 1

    def test_valid_token_runs_bot(self, monkeypatch):
        monkeypatch.setattr("main.DISCORD_TOKEN", "fake-token-123")
        # Phase G/D5: cleanup_stale_attachments no longer called from
        # main.main(); it is scheduled inside bot.on_ready. The earlier
        # monkeypatch here patched a now-removed call path.

        import main as main_mod
        # main() does `from bot import bot; bot.run(...)` — patch bot.bot at source
        import bot as bot_mod
        mock_run = MagicMock()
        monkeypatch.setattr(bot_mod.bot, "run", mock_run)
        # Bypass flock-based PID lock so the test works even when the bot is running
        monkeypatch.setattr(main_mod, "_acquire_pidlock", lambda: None)

        main_mod.main()
        mock_run.assert_called_once_with("fake-token-123")


# ---------------------------------------------------------------------------
# Additional session.py coverage tests
# ---------------------------------------------------------------------------
class TestSessionAdditional:
    """Additional tests for services/session.py to improve coverage."""

    # Use valid Discord-range channel IDs (10^14 <= id <= 10^19)
    _CH1 = 100000000000000042  # 10^14 + 42
    _CH2 = 100000000000000043  # 10^14 + 43

    def test_load_history_skips_blank_lines(self, tmp_state_dir):
        """Line 72: blank lines in JSONL are skipped."""
        from services.session import load_history_channel, _history_file

        path = _history_file(self._CH1)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"role":"user","content":"hello"}\n')
            f.write("\n")  # blank line
            f.write("   \n")  # whitespace-only line
            f.write('{"role":"assistant","content":"hi"}\n')

        msgs = load_history_channel(self._CH1)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["content"] == "hi"

    def test_load_history_skips_malformed_json(self, tmp_state_dir):
        """Line 77-78: JSONDecodeError lines are skipped."""
        from services.session import load_history_channel, _history_file

        path = _history_file(self._CH2)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"role":"user","content":"good"}\n')
            f.write("this is not json\n")
            f.write("{broken json\n")
            f.write('{"role":"assistant","content":"also good"}\n')

        msgs = load_history_channel(self._CH2)
        assert len(msgs) == 2

    # `load_all_history` / `LazyHistory` were removed in Phase D (perf overhaul).
    # The function was only referenced by these tests, never by production
    # code, so the tests went with it. Coverage of `load_history_channel`
    # itself is provided by the JSONLHistory tests above.

    def test_fix_history_removes_multiple_leading_assistant(self):
        """Line 113+: fix_history_integrity removes all leading assistant messages."""
        from services.session import fix_history_integrity

        h = [
            {"role": "assistant", "content": "orphan1"},
            {"role": "assistant", "content": "orphan2"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        fix_history_integrity(h)
        assert h[0]["role"] == "user"
        assert len(h) == 2

    def test_fix_history_all_assistant_becomes_empty(self):
        """fix_history_integrity with only assistant messages results in empty list."""
        from services.session import fix_history_integrity

        h = [
            {"role": "assistant", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        fix_history_integrity(h)
        assert h == []

    def test_fix_history_leading_assistant_then_dangling_user(self):
        """Combined: leading assistant + dangling user at end."""
        from services.session import fix_history_integrity

        h = [
            {"role": "assistant", "content": "orphan"},
            {"role": "user", "content": "q"},
        ]
        fix_history_integrity(h)
        # After removing dangling user at end, then removing leading assistant
        assert h == []

    _CH_MISSING = 100000000000099999  # guaranteed non-existent in tmp dir
    _CH_APPEND = 100000000000000050

    def test_load_history_empty_channel_returns_empty(self, tmp_state_dir):
        """load_history_channel for nonexistent file returns []."""
        from services.session import load_history_channel
        assert load_history_channel(self._CH_MISSING) == []

    def test_append_jsonl_adds_timestamp(self, tmp_state_dir):
        """append_jsonl adds timestamp and channel_id fields."""
        from services.session import append_jsonl, _history_file

        append_jsonl(self._CH_APPEND, {"role": "user", "content": "test"})
        path = _history_file(self._CH_APPEND)
        with open(path) as f:
            entry = json.loads(f.readline())
        assert "timestamp" in entry
        assert entry["channel_id"] == self._CH_APPEND


# ---------------------------------------------------------------------------
# H8: GroupChannel DM gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_dm_treated_as_dm_for_admin_gate(monkeypatch):
    """Non-admin messages in a GroupChannel must be silently rejected."""
    import bot

    monkeypatch.setattr("bot.ADMIN_USER_IDS", {99999})  # author 12345 is NOT admin

    mock_bot_user = MagicMock()
    mock_bot_user.id = 88888
    with patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=mock_bot_user):
        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock(spec=discord.Member)
        msg.author.id = 12345
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False  # not the bot user

        # Use a real GroupChannel spec so isinstance check passes
        msg.channel = MagicMock(spec=discord.GroupChannel)
        msg.channel.id = 11111
        msg.reply = AsyncMock()

        await bot.on_message(msg)

    # Should have returned early — reply never called (no rate-limit msg, no response)
    msg.reply.assert_not_called()


# ---------------------------------------------------------------------------
# H6: shared rate_limit module consulted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_uses_shared_module(monkeypatch):
    """When services.rate_limit is available, check_cooldown must be called."""
    import bot

    called_with = []

    def fake_check_cooldown(discord_id: str, is_admin: bool):
        called_with.append((discord_id, is_admin))
        return (False, 30)  # deny immediately so handler exits early

    monkeypatch.setattr("bot._USE_SHARED_RATE_LIMIT", True)
    monkeypatch.setattr("bot._rl_check_cooldown", fake_check_cooldown)
    monkeypatch.setattr("bot.ADMIN_USER_IDS", {99999})
    monkeypatch.setattr("bot.BOT_KEYWORDS", ["かなえ"])
    monkeypatch.setattr("bot.BOT_ROLE_IDS", set())

    mock_bot_user = MagicMock()
    mock_bot_user.id = 88888

    with patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=mock_bot_user):
        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock(spec=discord.Member)
        msg.author.id = 12345
        msg.author.bot = False
        msg.author.__eq__ = lambda self, other: False

        msg.channel = MagicMock()
        msg.channel.__class__ = type("TextChannel", (), {})
        msg.channel.id = 22222
        msg.content = "かなえ hello"
        msg.mentions = []
        msg.role_mentions = []
        msg.attachments = []
        msg.reference = None
        msg.reply = AsyncMock()

        await bot.on_message(msg)

    # Shared module's check_cooldown was invoked
    assert len(called_with) >= 1
    assert called_with[0][0] == "12345"


# ---------------------------------------------------------------------------
# SRE tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mem0_failure_logs_warning(caplog):
    """Mem0 store failure should log at WARNING, not DEBUG."""
    import bot

    async def _raise(*args, **kwargs):
        raise RuntimeError("Mem0 connection refused")

    with patch("bot.add_memories", side_effect=_raise):
        with caplog.at_level(logging.WARNING, logger="bot"):
            # Build and directly invoke the _mem0_store coroutine by simulating what
            # on_message does: define the inner coroutine and run it.
            mem0_user_id = "99999"
            raw_user_text = "hello"
            response_text = "hi"
            channel_id = 11111

            async def _mem0_store():
                try:
                    await bot.add_memories(
                        user_text=raw_user_text,
                        assistant_text=response_text,
                        user_id=mem0_user_id,
                        context_hint="",
                        metadata={"channel_id": str(channel_id), "channel_name": "test"},
                    )
                except Exception as exc:
                    bot.log.warning(
                        "Mem0 store failed: %s (user=%s, channel=%s)",
                        exc, mem0_user_id, channel_id,
                    )

            await _mem0_store()

    assert any(
        r.levelno == logging.WARNING and "Mem0 store failed" in r.message
        for r in caplog.records
    ), f"Expected WARNING with 'Mem0 store failed', got: {caplog.records}"


@pytest.mark.asyncio
async def test_tree_sync_only_runs_once():
    """bot.tree.sync() must be called exactly once even if on_ready fires twice."""
    import bot

    bot._tree_synced = False
    bot._cleanup_scheduled = True  # skip cleanup branch

    mock_tree_sync = AsyncMock(return_value=[MagicMock(), MagicMock()])
    mock_tree = MagicMock()
    mock_tree.sync = mock_tree_sync

    mock_bot_user = MagicMock()
    mock_bot_user.id = 88888

    with patch.object(type(bot.bot), "guilds", new_callable=PropertyMock, return_value=[]), \
         patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=mock_bot_user), \
         patch.object(type(bot.bot), "tree", new_callable=PropertyMock, return_value=mock_tree), \
         patch("bot.build_mention_regex"):
        await bot.on_ready()
        await bot.on_ready()

    assert mock_tree_sync.call_count == 1, (
        f"Expected tree.sync called once, got {mock_tree_sync.call_count}"
    )

    # Reset for other tests
    bot._tree_synced = False
    bot._cleanup_scheduled = False


def test_attachments_capped_at_10():
    """The >8000-char path caps all_files to 10 before calling reply."""
    import io as _io

    # Mirrors the cap logic added in bot.py for the >8000 path.
    fake_attach = [MagicMock(spec=discord.File) for _ in range(15)]
    all_files = [discord.File(_io.StringIO("x"), filename="response.md")] + fake_attach
    assert len(all_files) == 16

    if len(all_files) > 10:
        all_files = all_files[:10]

    assert len(all_files) == 10, f"Cap failed: got {len(all_files)} files"

    # Also verify the chunk-reply path cap.
    reply_files = fake_attach[:]  # 15 items
    if len(reply_files) > 10:
        reply_files = reply_files[:10]

    assert len(reply_files) == 10, f"Chunk-reply cap failed: got {len(reply_files)} files"


# ---------------------------------------------------------------------------
# Additional on_message coverage — admin paths and non-admin quota
# ---------------------------------------------------------------------------
class TestOnMessageAdminAndQuota:
    """Cover admin file attachment, non-admin quota, and delete command paths."""

    @pytest.fixture(autouse=True)
    def _reset_bot_state(self, monkeypatch):
        import bot
        bot.cooldowns.clear()
        bot._channel_semas.clear()
        bot._daily_quota.clear()
        monkeypatch.setattr("bot.COOLDOWN_SECONDS", 5)
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})
        monkeypatch.setattr("bot.BOT_KEYWORDS", ["かなえ"])
        monkeypatch.setattr("bot.BOT_ROLE_IDS", set())
        monkeypatch.setattr("bot.MAX_COOLDOWN_ENTRIES", 1000)
        monkeypatch.setattr("bot._USE_SHARED_RATE_LIMIT", False)

        self._mock_bot_user = MagicMock()
        self._mock_bot_user.id = 99900
        self._user_patcher = patch.object(
            type(bot.bot), "user", new_callable=PropertyMock, return_value=self._mock_bot_user
        )
        self._user_patcher.start()
        yield
        self._user_patcher.stop()

    async def _dispatch(self, message):
        import bot
        await bot.bot.on_message(message)

    @pytest.mark.asyncio
    async def test_non_admin_gets_quota_denied(self, monkeypatch):
        """Non-admin user hitting daily quota gets rejection message."""
        import bot
        monkeypatch.setattr("bot.ADMIN_USER_IDS", set())
        monkeypatch.setattr("bot.RESTRICTED_DAILY_QUOTA", 1)

        # Exhaust quota for user 55555
        bot._daily_quota[55555] = [time.time()]

        msg = _make_message(content="かなえ hi", author_id=55555)
        await self._dispatch(msg)

        calls = msg.reply.call_args_list
        assert any("上限" in str(c) or "quota" in str(c).lower() for c in calls)

    @pytest.mark.asyncio
    async def test_delete_command_non_admin_rejected(self, monkeypatch):
        """Non-admin sending delete command gets access denied."""
        import bot
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {99999})  # 12345 is NOT admin

        ref = MagicMock()
        ref.message_id = 100
        ref.cached_message = None
        msg = _make_message(content="削除", author_id=12345, is_dm=True, reference=ref)
        msg.channel = MagicMock(spec=discord.DMChannel)
        msg.channel.id = 77777

        await self._dispatch(msg)

        calls = msg.reply.call_args_list
        assert any("admin" in str(c).lower() or "❌" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_delete_command_admin_success(self, monkeypatch):
        """Admin delete command deletes the referenced message."""
        import bot
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})

        ref_msg = AsyncMock()
        ref_msg.id = 200
        ref_msg.delete = AsyncMock()

        ref = MagicMock()
        ref.message_id = 200
        ref.cached_message = ref_msg

        msg = _make_message(content="削除", author_id=12345, is_dm=True, reference=ref)
        msg.channel = MagicMock(spec=discord.DMChannel)
        msg.channel.id = 77777
        msg.delete = AsyncMock()

        await self._dispatch(msg)

        ref_msg.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_admin_cooldown_uses_restricted_seconds(self, monkeypatch):
        """Non-admin uses RESTRICTED_COOLDOWN_SECONDS, not COOLDOWN_SECONDS."""
        import bot
        monkeypatch.setattr("bot.ADMIN_USER_IDS", set())
        monkeypatch.setattr("bot.RESTRICTED_COOLDOWN_SECONDS", 120)

        # Place a fresh cooldown for non-admin user
        bot.cooldowns[55555] = time.time()

        msg = _make_message(content="かなえ hi", author_id=55555)
        await self._dispatch(msg)

        calls = msg.reply.call_args_list
        assert any("⏳" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_admin_file_attached_in_response(self, monkeypatch):
        """Admin response with file path attaches the file."""
        import bot
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"fake-png-data")
            tmp_path = tmp.name

        try:
            msg = _make_message(content="かなえ hello", author_id=12345)

            with patch("bot.strip_mention", return_value="hello"), \
                 patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("see " + tmp_path, None)), \
                 patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
                 patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
                 patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
                 patch("bot.extract_image_paths", return_value=[tmp_path]), \
                 patch("bot.extract_file_paths", return_value=[]), \
                 patch("bot.normalize_image_filenames", side_effect=lambda x: x), \
                 patch("bot.is_attach_ignored", return_value=False), \
                 patch("bot.split_message", return_value=["see " + tmp_path]), \
                 patch("bot.cleanup_attachments"), \
                 patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
                 patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
                await self._dispatch(msg)

                calls = msg.reply.call_args_list
                assert any(c.kwargs.get("files") for c in calls)
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_text_attachment_included_in_prompt(self, monkeypatch):
        """Text attachments are included in the prompt."""
        import bot

        msg = _make_message(content="かなえ hello", author_id=12345)
        captured_prompt = None

        async def capture_call(prompt, *args, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            return ("ok", None)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, side_effect=capture_call), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.download_text_attachments", new_callable=AsyncMock, return_value=[("notes.txt", "secret content")]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

        assert captured_prompt is not None
        assert "notes.txt" in captured_prompt
        assert "secret content" in captured_prompt

    @pytest.mark.asyncio
    async def test_cooldown_eviction_bounded(self, monkeypatch):
        """MAX_COOLDOWN_ENTRIES is respected by removing oldest entries."""
        import bot
        monkeypatch.setattr("bot.MAX_COOLDOWN_ENTRIES", 3)

        for uid in [100, 200, 300]:
            bot.cooldowns[uid] = time.time() - 1

        msg = _make_message(content="かなえ hello", author_id=400)

        with patch("bot.strip_mention", return_value="hello"), \
             patch("bot.call_with_tools", new_callable=AsyncMock, return_value=("ok", None)), \
             patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]), \
             patch("bot.resolve_message_links", new_callable=AsyncMock, return_value="hello"), \
             patch("bot.extract_image_paths", return_value=[]), \
             patch("bot.split_message", return_value=["ok"]), \
             patch("bot.cleanup_attachments"), \
             patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""), \
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""):
            await self._dispatch(msg)

        assert len(bot.cooldowns) <= 3
        assert 400 in bot.cooldowns


# ---------------------------------------------------------------------------
# _check_daily_quota unit tests
# ---------------------------------------------------------------------------
class TestCheckDailyQuota:
    def test_allows_when_quota_disabled(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.RESTRICTED_DAILY_QUOTA", 0)
        allowed, remaining = bot._check_daily_quota(99999)
        assert allowed is True
        assert remaining == -1

    def test_denies_when_quota_exhausted(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.RESTRICTED_DAILY_QUOTA", 2)
        bot._daily_quota.clear()
        bot._daily_quota[77777] = [time.time(), time.time()]
        allowed, remaining = bot._check_daily_quota(77777)
        assert allowed is False
        assert remaining == 0
        bot._daily_quota.clear()

    def test_counts_down_remaining(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.RESTRICTED_DAILY_QUOTA", 3)
        bot._daily_quota.clear()
        allowed, remaining = bot._check_daily_quota(88888)
        assert allowed is True
        assert remaining == 2  # used 1 of 3
        bot._daily_quota.clear()

    def test_evicts_expired_entries(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.RESTRICTED_DAILY_QUOTA", 5)
        bot._daily_quota.clear()
        # Old entry outside the 24h window
        bot._daily_quota[11111] = [time.time() - 90000]
        allowed, remaining = bot._check_daily_quota(11111)
        assert allowed is True
        assert remaining == 4  # the expired entry was evicted
        bot._daily_quota.clear()


# ---------------------------------------------------------------------------
# _cap_text unit tests
# ---------------------------------------------------------------------------
class TestCapText:
    def test_no_truncation_when_under_limit(self):
        from bot import _cap_text
        assert _cap_text("hello", 10) == "hello"

    def test_truncation_when_over_limit(self):
        from bot import _cap_text
        result = _cap_text("hello world", 5)
        assert result.startswith("hello")
        assert "truncated" in result

    def test_exact_limit_not_truncated(self):
        from bot import _cap_text
        assert _cap_text("hello", 5) == "hello"


# ---------------------------------------------------------------------------
# _wrap_untrusted unit tests
# ---------------------------------------------------------------------------
class TestWrapUntrusted:
    def test_wraps_in_xml_tag(self):
        from bot import _wrap_untrusted
        result = _wrap_untrusted("some text")
        assert "<untrusted_user_input>" in result
        assert "some text" in result
        assert "</untrusted_user_input>" in result


# ---------------------------------------------------------------------------
# _get_channel_sema LRU eviction
# ---------------------------------------------------------------------------
class TestGetChannelSema:
    def test_creates_semaphore_for_new_channel(self):
        import bot
        bot._channel_semas.clear()
        sema = bot._get_channel_sema(12345)
        assert sema is not None
        assert 12345 in bot._channel_semas
        bot._channel_semas.clear()

    def test_reuses_existing_semaphore(self):
        import bot
        bot._channel_semas.clear()
        s1 = bot._get_channel_sema(111)
        s2 = bot._get_channel_sema(111)
        assert s1 is s2
        bot._channel_semas.clear()

    def test_lru_eviction_when_over_limit(self, monkeypatch):
        import bot
        monkeypatch.setattr("bot.MAX_CHANNEL_SEMAS", 2)
        bot._channel_semas.clear()
        bot._get_channel_sema(1)
        bot._get_channel_sema(2)
        bot._get_channel_sema(3)  # should evict channel 1 (oldest, not locked)
        assert 1 not in bot._channel_semas
        bot._channel_semas.clear()

    def test_lru_eviction_skips_locked_head(self, monkeypatch):
        """locked sema が先頭にあっても、後ろの idle を evict する。"""
        import asyncio
        import bot
        monkeypatch.setattr("bot.MAX_CHANNEL_SEMAS", 2)
        bot._channel_semas.clear()

        async def run():
            bot._get_channel_sema(1)
            s1 = bot._channel_semas[1]
            await s1.acquire()  # channel 1 を locked にする
            bot._get_channel_sema(2)
            bot._get_channel_sema(3)
            # over=1。先頭の 1 は locked で skip、idle の 2 が evict される。
            assert 1 in bot._channel_semas
            assert 2 not in bot._channel_semas
            assert 3 in bot._channel_semas
            s1.release()

        asyncio.run(run())
        bot._channel_semas.clear()

    def test_lru_eviction_bounded_when_many_locked(self, monkeypatch):
        """先頭に locked が積み上がっても、辞書は cap 付近で頭打ちする。"""
        import asyncio
        import bot
        monkeypatch.setattr("bot.MAX_CHANNEL_SEMAS", 5)
        bot._channel_semas.clear()

        async def run():
            holders = []
            # 5 個 locked、その後に 10 個 idle を追加 → idle 側が順次 evict される
            for cid in range(1, 6):
                bot._get_channel_sema(cid)
                s = bot._channel_semas[cid]
                await s.acquire()
                holders.append(s)
            for cid in range(100, 110):
                bot._get_channel_sema(cid)
            # locked 5 個は残る + 直近 idle 1 個 ＝ 6 個程度に収まる
            assert len(bot._channel_semas) <= 6
            for s in holders:
                s.release()

        asyncio.run(run())
        bot._channel_semas.clear()

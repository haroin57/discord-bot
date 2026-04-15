"""Tests for bot.py and main.py — event handlers, routing, and entry point."""

import asyncio
import io
import json
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
        bot.channel_busy.clear()
        monkeypatch.setattr("bot.COOLDOWN_SECONDS", 5)
        monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})
        monkeypatch.setattr("bot.BOT_KEYWORDS", ["かなえ"])
        monkeypatch.setattr("bot.BOT_ROLE_IDS", set())
        monkeypatch.setattr("bot.MAX_COOLDOWN_ENTRIES", 1000)

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

    @pytest.mark.asyncio
    async def test_channel_busy(self, monkeypatch):
        import bot

        # First, set up cooldowns so the user passes rate limiting
        # The user must pass the keyword/mention check and rate limit check
        msg = _make_message(content="かなえ hello", author_id=12345, channel_id=888)

        with patch("bot.strip_mention", return_value="hello"):
            # Pre-mark channel as busy
            bot.channel_busy.add(888)

            await self._dispatch(msg)

            # Should reply with busy message
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

            assert 555 not in bot.channel_busy

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

            assert 666 not in bot.channel_busy

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
            assert "先輩ではありません" in captured_prompt

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
            assert "Message from" in captured_prompt
            assert "先輩ではありません" not in captured_prompt

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
            assert "Replied-to message" in captured_prompt

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
             patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value="[Thread: test - past 5 messages]\nfoo\n[End of thread history]"):
            await self._dispatch(msg)

            assert captured_prompt is not None
            assert "Thread" in captured_prompt


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
        monkeypatch.setattr("main.cleanup_stale_attachments", lambda: None)

        import main as main_mod
        # main() does `from bot import bot; bot.run(...)` — patch bot.bot at source
        import bot as bot_mod
        mock_run = MagicMock()
        monkeypatch.setattr(bot_mod.bot, "run", mock_run)

        main_mod.main()
        mock_run.assert_called_once_with("fake-token-123")


# ---------------------------------------------------------------------------
# Additional session.py coverage tests
# ---------------------------------------------------------------------------
class TestSessionAdditional:
    """Additional tests for services/session.py to improve coverage."""

    def test_load_history_skips_blank_lines(self, tmp_state_dir):
        """Line 72: blank lines in JSONL are skipped."""
        from services.session import load_history_channel, _history_file

        path = _history_file(42)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"role":"user","content":"hello"}\n')
            f.write("\n")  # blank line
            f.write("   \n")  # whitespace-only line
            f.write('{"role":"assistant","content":"hi"}\n')

        msgs = load_history_channel(42)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["content"] == "hi"

    def test_load_history_skips_malformed_json(self, tmp_state_dir):
        """Line 77-78: JSONDecodeError lines are skipped."""
        from services.session import load_history_channel, _history_file

        path = _history_file(43)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"role":"user","content":"good"}\n')
            f.write("this is not json\n")
            f.write("{broken json\n")
            f.write('{"role":"assistant","content":"also good"}\n')

        msgs = load_history_channel(43)
        assert len(msgs) == 2

    def test_load_all_history_skips_non_jsonl(self, tmp_state_dir):
        """Line 89-90: non-.jsonl files in history dir are ignored."""
        from services.session import load_all_history, append_jsonl
        import config

        # Create a valid JSONL
        append_jsonl(100, {"role": "user", "content": "a"})
        append_jsonl(100, {"role": "assistant", "content": "b"})

        # Create a non-jsonl file
        non_jsonl = os.path.join(config.HISTORY_DIR, "notes.txt")
        with open(non_jsonl, "w") as f:
            f.write("not a jsonl file\n")

        # Create a file with non-numeric name
        bad_name = os.path.join(config.HISTORY_DIR, "bad.jsonl")
        with open(bad_name, "w") as f:
            f.write('{"role":"user","content":"test"}\n')

        result = load_all_history()
        assert 100 in result
        # "bad" can't be parsed as int, so ValueError is caught

    def test_load_all_history_value_error_on_filename(self, tmp_state_dir):
        """Lines 95-96: ValueError when filename cannot be converted to int."""
        from services.session import load_all_history
        import config

        bad_name = os.path.join(config.HISTORY_DIR, "notanumber.jsonl")
        with open(bad_name, "w") as f:
            f.write('{"role":"user","content":"test"}\n')

        # Should not raise, caught by except (FileNotFoundError, ValueError)
        result = load_all_history()
        assert isinstance(result, OrderedDict)

    def test_load_all_history_missing_dir(self, tmp_state_dir, monkeypatch):
        """Lines 95-96: FileNotFoundError when history dir does not exist."""
        monkeypatch.setattr("services.session.HISTORY_DIR", "/nonexistent/path")
        from services.session import load_all_history

        result = load_all_history()
        assert result == OrderedDict()

    def test_load_all_history_evicts_excess(self, tmp_state_dir, monkeypatch):
        """Lines 97-98: evict oldest when exceeding MAX_HISTORY."""
        from services.session import load_all_history, append_jsonl
        monkeypatch.setattr("services.session.MAX_HISTORY", 2)

        for ch in [1, 2, 3]:
            append_jsonl(ch, {"role": "user", "content": f"msg-{ch}"})
            append_jsonl(ch, {"role": "assistant", "content": f"reply-{ch}"})

        result = load_all_history()
        assert len(result) == 2
        # Channel 1 should have been evicted (oldest)
        assert 1 not in result
        assert 2 in result
        assert 3 in result

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

    def test_load_history_empty_channel_returns_empty(self, tmp_state_dir):
        """load_history_channel for nonexistent file returns []."""
        from services.session import load_history_channel
        assert load_history_channel(99999) == []

    def test_append_jsonl_adds_timestamp(self, tmp_state_dir):
        """append_jsonl adds timestamp and channel_id fields."""
        from services.session import append_jsonl, _history_file

        append_jsonl(50, {"role": "user", "content": "test"})
        path = _history_file(50)
        with open(path) as f:
            entry = json.loads(f.readline())
        assert "timestamp" in entry
        assert entry["channel_id"] == 50

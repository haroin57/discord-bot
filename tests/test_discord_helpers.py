"""Tests for utils/discord_helpers.py — split_message, strip_mention, format_embed, resolve_message_links."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import BOT_ROLE_IDS
from utils.discord_helpers import (
    build_mention_regex,
    format_embed,
    resolve_message_links,
    split_message,
    strip_mention,
    DISCORD_MSG_URL_RE,
)


class TestSplitMessage:
    def test_short_message(self):
        assert split_message("hello") == ["hello"]

    def test_splits_on_newline(self):
        text = "a" * 1800 + "\n" + "b" * 400
        chunks = split_message(text, limit=2000)
        assert len(chunks) == 2
        assert len(chunks[0]) <= 2000

    def test_splits_on_space(self):
        text = "word " * 500
        chunks = split_message(text, limit=2000)
        assert all(len(c) <= 2000 for c in chunks)

    def test_hard_split_no_separator(self):
        text = "x" * 3000
        chunks = split_message(text, limit=2000)
        assert len(chunks) == 2
        assert chunks[0] == "x" * 2000

    def test_empty_string(self):
        assert split_message("") == [""]

    def test_exact_limit(self):
        text = "a" * 2000
        assert split_message(text, limit=2000) == [text]

    def test_multiple_splits(self):
        text = "a" * 5000
        chunks = split_message(text, limit=2000)
        assert len(chunks) == 3
        assert "".join(chunks) == text


class TestStripMention:
    def test_strips_direct_mention(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("<@123456> hello", 123456)
        assert result == "hello"

    def test_strips_nick_mention(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("<@!123456> hello", 123456)
        assert result == "hello"

    def test_strips_role_mention(self):
        BOT_ROLE_IDS.clear()
        BOT_ROLE_IDS.add(789)
        build_mention_regex(123456)
        result = strip_mention("<@&789> hello", 123456)
        assert result == "hello"

    def test_strips_keyword(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("かなえ、元気？", 123456)
        assert "かなえ" not in result
        assert "元気" in result

    def test_preserves_other_text(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("<@123456> これは大事なメッセージ", 123456)
        assert result == "これは大事なメッセージ"

    def test_no_mention_unchanged(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("普通のメッセージ", 123456)
        assert result == "普通のメッセージ"


class TestFormatEmbed:
    def test_full_embed(self):
        embed = MagicMock()
        embed.title = "Title"
        embed.description = "Desc"
        embed.fields = [MagicMock(name="Field1", value="Val1")]
        embed.footer = MagicMock(text="Footer")
        result = format_embed(embed)
        assert "**Title**" in result
        assert "Desc" in result
        assert "(Footer)" in result

    def test_minimal_embed(self):
        embed = MagicMock()
        embed.title = None
        embed.description = "Just desc"
        embed.fields = []
        embed.footer = None
        result = format_embed(embed)
        assert result == "Just desc"

    def test_empty_embed(self):
        embed = MagicMock()
        embed.title = None
        embed.description = None
        embed.fields = []
        embed.footer = None
        result = format_embed(embed)
        assert result == ""


class TestDiscordMsgUrlRegex:
    def test_matches_standard_url(self):
        url = "https://discord.com/channels/111/222/333"
        m = DISCORD_MSG_URL_RE.search(url)
        assert m
        assert m.group(1) == "111"
        assert m.group(2) == "222"
        assert m.group(3) == "333"

    def test_matches_ptb_url(self):
        url = "https://ptb.discord.com/channels/111/222/333"
        assert DISCORD_MSG_URL_RE.search(url)

    def test_matches_canary_url(self):
        url = "https://canary.discord.com/channels/111/222/333"
        assert DISCORD_MSG_URL_RE.search(url)

    def test_matches_discordapp_url(self):
        url = "https://discordapp.com/channels/111/222/333"
        assert DISCORD_MSG_URL_RE.search(url)

    def test_no_match_on_random_text(self):
        assert DISCORD_MSG_URL_RE.search("hello world") is None


class TestResolveMessageLinks:
    @pytest.mark.asyncio
    async def test_no_links(self):
        result = await resolve_message_links(MagicMock(), "no links here")
        assert result == "no links here"

    @pytest.mark.asyncio
    async def test_resolves_link(self):
        mock_client = MagicMock()
        mock_channel = MagicMock()
        mock_msg = MagicMock()
        mock_msg.author.display_name = "TestUser"
        mock_msg.content = "referenced content"
        mock_msg.embeds = []
        mock_msg.attachments = []
        mock_channel.name = "general"
        # guild=None avoids the guild-id equality check; requester=None
        # skips the confused-deputy read-permission check.
        mock_channel.guild = None

        mock_client.get_channel = MagicMock(return_value=mock_channel)
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)

        text = "Check this: https://discord.com/channels/111/222/333"
        result = await resolve_message_links(mock_client, text, requester=None)
        assert "TestUser" in result
        assert "referenced content" in result

    @pytest.mark.asyncio
    async def test_failed_resolution_keeps_url(self):
        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=None)
        mock_client.fetch_channel = AsyncMock(side_effect=Exception("Not found"))

        text = "Check: https://discord.com/channels/111/222/333"
        result = await resolve_message_links(mock_client, text)
        assert "discord.com/channels/111/222/333" in result

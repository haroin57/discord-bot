"""Tests for utils/discord_helpers.py — split_message, strip_mention, format_embed, resolve_message_links."""

import asyncio
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from config import ADMIN_USER_IDS, BOT_IGNORED_ROLE_IDS, BOT_ROLE_IDS
from utils.discord_helpers import (
    _cache_get,
    _cache_set,
    _format_msg_body,
    _parse_and_verify_url,
    _requester_can_read,
    _resolve_one_link,
    _should_include_msg,
    _summarize_message,
    build_mention_regex,
    fetch_channel_context,
    fetch_thread_context,
    format_embed,
    resolve_channel_mention_contexts,
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
        assert all(len(c) <= 2000 for c in chunks)
        # content survives minus the part markers
        joined = "".join(c.split("\n-# (")[0] for c in chunks)
        assert joined == text

    def test_empty_string(self):
        assert split_message("") == [""]

    def test_exact_limit(self):
        text = "a" * 2000
        assert split_message(text, limit=2000) == [text]

    def test_multiple_splits(self):
        text = "a" * 5000
        chunks = split_message(text, limit=2000)
        assert len(chunks) == 3
        joined = "".join(c.split("\n-# (")[0] for c in chunks)
        assert joined == text

    def test_part_markers_on_multichunk(self):
        text = ("line\n" * 200) + ("x" * 1500)
        chunks = split_message(text, limit=2000)
        n = len(chunks)
        assert n > 1
        for i, c in enumerate(chunks, 1):
            assert c.endswith(f"-# ({i}/{n})")
            assert len(c) <= 2000

    def test_single_chunk_has_no_marker(self):
        assert split_message("short") == ["short"]

    def test_numbered_false_keeps_content_joinable(self):
        text = "a" * 5000
        chunks = split_message(text, limit=2000, numbered=False)
        assert "".join(chunks) == text

    def test_fence_not_left_unbalanced(self):
        code = "\n".join(f"print({i})" for i in range(400))
        text = f"intro\n```python\n{code}\n```\noutro"
        chunks = split_message(text, limit=2000)
        assert len(chunks) > 1
        for c in chunks:
            body = c.split("\n-# (")[0]
            assert body.count("```") % 2 == 0, f"unbalanced fence in: {body[-80:]}"
        # continuation chunks reopen with the language tag
        assert any(
            c.split("\n-# (")[0].startswith("```python") for c in chunks[1:]
        )

    def test_fence_language_preserved_across_splits(self):
        long_line_code = "\n".join("x = " + "1" * 60 for _ in range(60))
        text = f"```js\n{long_line_code}\n```"
        chunks = split_message(text, limit=2000)
        for c in chunks[1:]:
            body = c.split("\n-# (")[0]
            assert body.startswith("```js")

    def test_unclosed_source_fence_passes_through(self):
        # The author's own unclosed fence is not a split artifact — the last
        # chunk may legitimately end inside it.
        text = "```python\n" + ("y = 2\n" * 500)
        chunks = split_message(text, limit=2000)
        for c in chunks[:-1]:
            body = c.split("\n-# (")[0]
            assert body.count("```") % 2 == 0


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

    def test_strips_ignored_role_mention(self):
        BOT_ROLE_IDS.clear()
        BOT_IGNORED_ROLE_IDS.clear()
        BOT_IGNORED_ROLE_IDS.add(987)
        try:
            build_mention_regex(123456)
            result = strip_mention("<@&987> hello", 123456)
            assert result == "hello"
        finally:
            BOT_IGNORED_ROLE_IDS.clear()

    def test_keyword_callout_preserved(self):
        # ADR-0003: キーワード呼びかけは strip しない。本文に残して Claude に渡す。
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("かなえ、元気？", 123456)
        assert result == "かなえ、元気？"

    def test_keyword_in_compound_word_preserved(self):
        # 事故再現ケース: 本文途中の「かなえ」が後続を食わないこと。
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("かなえちゃんによると新玉ねぎは", 123456)
        assert result == "かなえちゃんによると新玉ねぎは"

    def test_keyword_with_particle_preserved(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("かなえによると", 123456)
        assert result == "かなえによると"

    def test_mention_stripped_keyword_preserved(self):
        # メンショントークンは除去、本文中のキーワードは保持。
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("<@123456> かなえの音楽", 123456)
        assert result == "かなえの音楽"

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


class TestContextConstants:
    """Verify widened context constants are set correctly."""

    def test_layer1_count(self):
        from utils.discord_helpers import LAYER1_COUNT
        assert LAYER1_COUNT == 25

    def test_layer2_count(self):
        from utils.discord_helpers import LAYER2_COUNT
        assert LAYER2_COUNT == 15

    def test_max_context_chars(self):
        from utils.discord_helpers import MAX_CONTEXT_CHARS
        assert MAX_CONTEXT_CHARS == 20000

    def test_channel_context_limit_derived(self):
        from utils.discord_helpers import LAYER1_COUNT, LAYER2_COUNT, CHANNEL_CONTEXT_LIMIT
        assert CHANNEL_CONTEXT_LIMIT == LAYER1_COUNT + LAYER2_COUNT


class TestBuildLayeredLines:
    """Test _build_layered_lines with the new wider context settings."""

    def test_all_layer1_when_few_messages(self):
        from utils.discord_helpers import _build_layered_lines, LAYER1_COUNT
        messages = [(f"user{i}", f"message {i}") for i in range(5)]
        lines, _ = _build_layered_lines(messages)
        # All 5 should be full text (within LAYER1_COUNT=25)
        assert len(lines) == 5
        assert all("message" in line for line in lines)

    def test_layer2_summarized_when_many_messages(self):
        from utils.discord_helpers import _build_layered_lines, LAYER1_COUNT, LAYER2_COUNT
        total = LAYER1_COUNT + LAYER2_COUNT  # 40
        messages = [(f"user{i}", f"{'x' * 300} msg{i}") for i in range(total)]
        lines, _ = _build_layered_lines(messages)
        # Layer 2 messages (first 15) should be summarized (shorter)
        # Layer 1 messages (last 25) should be full
        assert len(lines) >= LAYER1_COUNT  # at least layer1 lines present


class TestCacheGetSet:
    def setup_method(self):
        # Clear cache before each test
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()

    def test_cache_miss_returns_none(self):
        assert _cache_get((1, 2, False)) is None

    def test_cache_set_and_get(self):
        _cache_set((1, 2, False), "hello")
        assert _cache_get((1, 2, False)) == "hello"

    def test_cache_expired_returns_none(self, monkeypatch):
        import utils.discord_helpers as dh
        import utils.cache as cache_mod
        from config import THREAD_CONTEXT_CACHE_TTL_S
        key = (3, 4, False)
        # Insert via the normal API, then jump the clock past TTL.
        _cache_set(key, "expired")
        original = cache_mod.time.monotonic
        monkeypatch.setattr(
            cache_mod.time, "monotonic",
            lambda: original() + THREAD_CONTEXT_CACHE_TTL_S + 1.0,
        )
        assert _cache_get(key) is None
        assert key not in dh._thread_context_cache

    def test_cache_evicts_lru_when_full(self):
        import utils.discord_helpers as dh
        from config import THREAD_CONTEXT_CACHE_MAX
        # Fill cache to max
        for i in range(THREAD_CONTEXT_CACHE_MAX):
            _cache_set((i, i, False), f"val{i}")
        assert len(dh._thread_context_cache) == THREAD_CONTEXT_CACHE_MAX
        # Add one more — should evict oldest
        _cache_set((9999, 9999, False), "new")
        assert len(dh._thread_context_cache) == THREAD_CONTEXT_CACHE_MAX
        assert (0, 0, False) not in dh._thread_context_cache

    def test_cache_key_includes_admin_flag(self):
        _cache_set((1, 2, True), "admin-result")
        _cache_set((1, 2, False), "nonadmin-result")
        assert _cache_get((1, 2, True)) == "admin-result"
        assert _cache_get((1, 2, False)) == "nonadmin-result"


class TestShouldIncludeMsg:
    def setup_method(self):
        ADMIN_USER_IDS.clear()

    def teardown_method(self):
        ADMIN_USER_IDS.clear()

    def test_admin_caller_sees_all(self):
        msg = MagicMock()
        msg.author.id = 9999
        ADMIN_USER_IDS.add(9999)
        assert _should_include_msg(msg, caller_is_admin=True) is True

    def test_non_admin_caller_filters_admin_messages(self):
        msg = MagicMock()
        msg.author.id = 9999
        ADMIN_USER_IDS.add(9999)
        assert _should_include_msg(msg, caller_is_admin=False) is False

    def test_non_admin_caller_sees_non_admin_messages(self):
        msg = MagicMock()
        msg.author.id = 1234
        ADMIN_USER_IDS.discard(1234)
        assert _should_include_msg(msg, caller_is_admin=False) is True


class TestFormatMsgBody:
    def test_content_only(self):
        msg = MagicMock()
        msg.content = "hello"
        msg.embeds = []
        msg.attachments = []
        assert _format_msg_body(msg) == "hello"

    def test_with_embed(self):
        embed = MagicMock()
        embed.title = "T"
        embed.description = "D"
        embed.fields = []
        embed.footer = None
        msg = MagicMock()
        msg.content = "text"
        msg.embeds = [embed]
        msg.attachments = []
        result = _format_msg_body(msg)
        assert "text" in result
        assert "**T**" in result

    def test_with_attachment(self):
        att = MagicMock()
        att.filename = "file.txt"
        msg = MagicMock()
        msg.content = ""
        msg.embeds = []
        msg.attachments = [att]
        result = _format_msg_body(msg)
        assert "[Attachment: file.txt]" in result

    def test_empty_message(self):
        msg = MagicMock()
        msg.content = ""
        msg.embeds = []
        msg.attachments = []
        assert _format_msg_body(msg) == ""


class TestSummarizeMessage:
    def test_short_message_unchanged(self):
        assert _summarize_message("short", max_chars=100) == "short"

    def test_long_message_truncated(self):
        long = "x" * 200
        result = _summarize_message(long, max_chars=50)
        assert result == "x" * 50 + " [...]"
        assert len(result) == 56  # 50 + len(" [...]")=6


class TestParseAndVerifyUrl:
    def test_valid_discord_url(self):
        result = _parse_and_verify_url("https://discord.com/channels/1/2/3")
        assert result == (1, 2, 3)

    def test_valid_ptb_url(self):
        result = _parse_and_verify_url("https://ptb.discord.com/channels/10/20/30")
        assert result == (10, 20, 30)

    def test_valid_canary_url(self):
        result = _parse_and_verify_url("https://canary.discord.com/channels/1/2/3")
        assert result == (1, 2, 3)

    def test_valid_discordapp_url(self):
        result = _parse_and_verify_url("https://discordapp.com/channels/1/2/3")
        assert result == (1, 2, 3)

    def test_invalid_host_returns_none(self):
        assert _parse_and_verify_url("https://evil.com/channels/1/2/3") is None

    def test_userinfo_spoofing_returns_none(self):
        # Classic SSRF spoof attempt
        assert _parse_and_verify_url("https://discord.com@evil.com/channels/1/2/3") is None

    def test_non_channels_path_returns_none(self):
        assert _parse_and_verify_url("https://discord.com/api/1/2/3") is None

    def test_non_integer_ids_returns_none(self):
        assert _parse_and_verify_url("https://discord.com/channels/a/b/c") is None

    def test_wrong_scheme_returns_none(self):
        assert _parse_and_verify_url("ftp://discord.com/channels/1/2/3") is None

    def test_too_few_path_parts_returns_none(self):
        assert _parse_and_verify_url("https://discord.com/channels/1/2") is None


class TestRequesterCanRead:
    def test_guild_member_with_perms(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild = MagicMock()
        member = MagicMock()
        channel.guild.get_member = MagicMock(return_value=member)
        perms = MagicMock()
        perms.read_message_history = True
        perms.view_channel = True
        channel.permissions_for = MagicMock(return_value=perms)

        requester = MagicMock()
        requester.id = 42
        assert _requester_can_read(channel, requester) is True

    def test_guild_member_without_perms(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild = MagicMock()
        member = MagicMock()
        channel.guild.get_member = MagicMock(return_value=member)
        perms = MagicMock()
        perms.read_message_history = False
        perms.view_channel = True
        channel.permissions_for = MagicMock(return_value=perms)

        requester = MagicMock()
        requester.id = 42
        assert _requester_can_read(channel, requester) is False

    def test_non_member_returns_false(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild = MagicMock()
        channel.guild.get_member = MagicMock(return_value=None)

        requester = MagicMock()
        requester.id = 42
        assert _requester_can_read(channel, requester) is False

    def test_dm_channel_matching_recipient(self):
        channel = MagicMock()
        channel.guild = None
        recipient = MagicMock()
        recipient.id = 42
        channel.recipient = recipient
        del channel.recipients  # ensure only `recipient` attr exists

        requester = MagicMock()
        requester.id = 42
        # Use a plain object without spec so attribute access works
        assert _requester_can_read(channel, requester) is True

    def test_dm_channel_wrong_recipient(self):
        channel = MagicMock()
        channel.guild = None
        recipient = MagicMock()
        recipient.id = 99
        channel.recipient = recipient
        del channel.recipients

        requester = MagicMock()
        requester.id = 42
        assert _requester_can_read(channel, requester) is False

    def test_no_permissions_for_attr_returns_false(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild = MagicMock()
        member = MagicMock()
        channel.guild.get_member = MagicMock(return_value=member)
        # permissions_for raises exception
        channel.permissions_for = MagicMock(side_effect=Exception("no perms"))

        requester = MagicMock()
        requester.id = 42
        assert _requester_can_read(channel, requester) is False


class TestFetchThreadContext:
    @pytest.mark.asyncio
    async def test_returns_cached_result(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 100
        before_msg = MagicMock()
        before_msg.id = 200

        # Pre-populate cache
        cache_key = (100, 200, False)
        dh._thread_context_cache.set(cache_key, "cached context")

        result = await fetch_thread_context(thread, before_msg, caller_is_admin=False)
        assert result == "cached context"

    @pytest.mark.asyncio
    async def test_empty_history_returns_empty_string(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 101
        thread.name = "test-thread"
        before_msg = MagicMock()
        before_msg.id = 201

        async def empty_history(**kwargs):
            return
            yield  # make it async generator

        thread.history = MagicMock(return_value=empty_history())

        result = await fetch_thread_context(thread, before_msg, caller_is_admin=False)
        assert result == ""

    @pytest.mark.asyncio
    async def test_admin_sees_admin_messages(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()
        ADMIN_USER_IDS.clear()
        ADMIN_USER_IDS.add(777)

        thread = MagicMock(spec=discord.Thread)
        thread.id = 102
        thread.name = "secret-thread"
        before_msg = MagicMock()
        before_msg.id = 202

        admin_msg = MagicMock()
        admin_msg.author.id = 777
        admin_msg.author.display_name = "AdminUser"
        admin_msg.content = "admin secret"
        admin_msg.embeds = []
        admin_msg.attachments = []

        async def history_gen(**kwargs):
            yield admin_msg

        thread.history = MagicMock(return_value=history_gen())

        result = await fetch_thread_context(thread, before_msg, caller_is_admin=True)
        assert "admin secret" in result
        ADMIN_USER_IDS.clear()

    @pytest.mark.asyncio
    async def test_non_admin_filters_admin_messages(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()
        ADMIN_USER_IDS.clear()
        ADMIN_USER_IDS.add(777)

        thread = MagicMock(spec=discord.Thread)
        thread.id = 103
        thread.name = "mixed-thread"
        before_msg = MagicMock()
        before_msg.id = 203

        admin_msg = MagicMock()
        admin_msg.author.id = 777
        admin_msg.author.display_name = "AdminUser"
        admin_msg.content = "admin secret"
        admin_msg.embeds = []
        admin_msg.attachments = []

        async def history_gen(**kwargs):
            yield admin_msg

        thread.history = MagicMock(return_value=history_gen())

        result = await fetch_thread_context(thread, before_msg, caller_is_admin=False)
        assert result == ""  # filtered out, nothing left
        ADMIN_USER_IDS.clear()

    @pytest.mark.asyncio
    async def test_thread_context_cache_partition_by_admin_flag(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 104
        thread.name = "partition-thread"
        before_msg = MagicMock()
        before_msg.id = 204

        # Pre-populate both admin and non-admin cache entries
        dh._thread_context_cache.set((104, 204, True), "admin-context")
        dh._thread_context_cache.set((104, 204, False), "nonadmin-context")

        admin_result = await fetch_thread_context(thread, before_msg, caller_is_admin=True)
        nonadmin_result = await fetch_thread_context(thread, before_msg, caller_is_admin=False)

        assert admin_result == "admin-context"
        assert nonadmin_result == "nonadmin-context"


class TestFetchChannelContext:
    @pytest.mark.asyncio
    async def test_returns_cached_result(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()

        channel = MagicMock()
        channel.id = 500
        before_msg = MagicMock()
        before_msg.id = 600

        dh._thread_context_cache.set((500, 600, False), "cached channel")
        result = await fetch_channel_context(channel, before_msg, caller_is_admin=False)
        assert result == "cached channel"

    @pytest.mark.asyncio
    async def test_empty_history_returns_empty(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()

        channel = MagicMock()
        channel.id = 501
        before_msg = MagicMock()
        before_msg.id = 601

        async def empty_history(**kwargs):
            return
            yield

        channel.history = MagicMock(return_value=empty_history())
        result = await fetch_channel_context(channel, before_msg, caller_is_admin=False)
        assert result == ""

    @pytest.mark.asyncio
    async def test_channel_context_non_admin_filters_admin(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()
        ADMIN_USER_IDS.clear()
        ADMIN_USER_IDS.add(888)

        channel = MagicMock()
        channel.id = 502
        before_msg = MagicMock()
        before_msg.id = 602

        admin_msg = MagicMock()
        admin_msg.author.id = 888
        admin_msg.author.display_name = "Admin"
        admin_msg.content = "private admin content"
        admin_msg.embeds = []
        admin_msg.attachments = []

        async def history_gen(**kwargs):
            yield admin_msg

        channel.history = MagicMock(return_value=history_gen())
        result = await fetch_channel_context(channel, before_msg, caller_is_admin=False)
        assert result == ""
        ADMIN_USER_IDS.clear()

    @pytest.mark.asyncio
    async def test_channel_context_with_messages(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()
        ADMIN_USER_IDS.clear()

        channel = MagicMock()
        channel.id = 503
        before_msg = MagicMock()
        before_msg.id = 603

        msg = MagicMock()
        msg.author.id = 1234
        msg.author.display_name = "User"
        msg.content = "hello channel"
        msg.embeds = []
        msg.attachments = []

        async def history_gen(**kwargs):
            yield msg

        channel.history = MagicMock(return_value=history_gen())
        result = await fetch_channel_context(channel, before_msg, caller_is_admin=False)
        assert "hello channel" in result
        assert "Channel recent" in result


class TestResolveOneLink:
    @pytest.mark.asyncio
    async def test_invalid_url_returns_none(self):
        match = MagicMock()
        match.group = MagicMock(return_value="https://evil.com/channels/1/2/3")
        client = MagicMock()
        _, result = await _resolve_one_link(client, match, requester=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_admin_bypasses_permission_check(self):
        import utils.discord_helpers as dh
        dh._thread_context_cache.clear()

        channel = MagicMock()
        channel.guild = MagicMock()
        channel.guild.id = 1
        channel.name = "general"

        msg = MagicMock()
        msg.author.display_name = "TestUser"
        msg.content = "admin content"
        msg.embeds = []
        msg.attachments = []

        channel.fetch_message = AsyncMock(return_value=msg)
        # Not a thread, so no history needed
        del channel.history

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        match = MagicMock()
        match.group = MagicMock(side_effect=lambda i: {
            0: "https://discord.com/channels/1/222/333",
            1: "1", 2: "222", 3: "333"
        }[i])

        _, result = await _resolve_one_link(client, match, requester=MagicMock(id=99), caller_is_admin=True)
        assert result is not None
        assert "admin content" in result

    @pytest.mark.asyncio
    async def test_non_admin_denied_without_read_permission(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild = MagicMock()
        channel.guild.id = 1
        channel.name = "restricted"
        member = MagicMock()
        channel.guild.get_member = MagicMock(return_value=member)
        perms = MagicMock()
        perms.read_message_history = False
        perms.view_channel = False
        channel.permissions_for = MagicMock(return_value=perms)

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        requester = MagicMock()
        requester.id = 55

        match = MagicMock()
        match.group = MagicMock(side_effect=lambda i: {
            0: "https://discord.com/channels/1/222/333",
            1: "1", 2: "222", 3: "333"
        }[i])

        _, result = await _resolve_one_link(client, match, requester=requester, caller_is_admin=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_guild_id_mismatch_returns_none(self):
        channel = MagicMock()
        channel.guild = MagicMock()
        channel.guild.id = 999  # Different from URL's guild_id=1

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        match = MagicMock()
        match.group = MagicMock(side_effect=lambda i: {
            0: "https://discord.com/channels/1/222/333",
            1: "1", 2: "222", 3: "333"
        }[i])

        _, result = await _resolve_one_link(client, match, requester=None, caller_is_admin=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_channel_none_returns_none(self):
        client = MagicMock()
        client.get_channel = MagicMock(return_value=None)
        client.fetch_channel = AsyncMock(return_value=None)

        match = MagicMock()
        match.group = MagicMock(side_effect=lambda i: {
            0: "https://discord.com/channels/1/222/333",
            1: "1", 2: "222", 3: "333"
        }[i])

        _, result = await _resolve_one_link(client, match, requester=None, caller_is_admin=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_thread_context_fetched_on_thread_channel(self):
        channel = MagicMock(spec=discord.Thread)
        channel.guild = None
        channel.name = "my-thread"
        channel.id = 222

        msg = MagicMock()
        msg.author.display_name = "Writer"
        msg.content = "thread content"
        msg.embeds = []
        msg.attachments = []

        channel.fetch_message = AsyncMock(return_value=msg)

        async def fake_history(**kwargs):
            return
            yield

        channel.history = MagicMock(side_effect=lambda **kwargs: fake_history())

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        match = MagicMock()
        match.group = MagicMock(side_effect=lambda i: {
            0: "https://discord.com/channels/1/222/333",
            1: "1", 2: "222", 3: "333"
        }[i])

        _, result = await _resolve_one_link(client, match, requester=None, caller_is_admin=True)
        assert result is not None
        assert "thread content" in result


class TestResolveMessageLinksExtended:
    @pytest.mark.asyncio
    async def test_admin_can_resolve_links(self):
        mock_client = MagicMock()
        channel = MagicMock()
        channel.guild = None
        channel.name = "admin-ch"

        msg = MagicMock()
        msg.author.display_name = "AdminUser"
        msg.content = "privileged content"
        msg.embeds = []
        msg.attachments = []

        mock_client.get_channel = MagicMock(return_value=channel)
        channel.fetch_message = AsyncMock(return_value=msg)

        text = "See: https://discord.com/channels/1/2/3"
        result = await resolve_message_links(mock_client, text, requester=None, caller_is_admin=True)
        assert "privileged content" in result

    @pytest.mark.asyncio
    async def test_max_resolved_links_capped(self):
        from config import MAX_RESOLVED_LINKS
        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=None)
        mock_client.fetch_channel = AsyncMock(return_value=None)

        # Build text with more links than the cap
        links = " ".join(
            f"https://discord.com/channels/1/2/{i}"
            for i in range(MAX_RESOLVED_LINKS + 5)
        )
        result = await resolve_message_links(mock_client, links)
        # All unresolved links should remain (channel was None)
        assert "discord.com/channels" in result


class TestResolveChannelMentionContexts:
    @pytest.mark.asyncio
    async def test_no_mentions_returns_empty(self):
        client = MagicMock()
        result = await resolve_channel_mention_contexts(
            client, "no mentions here", current_channel_id=1
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_current_channel_skipped(self):
        client = MagicMock()
        result = await resolve_channel_mention_contexts(
            client, "<#100>", current_channel_id=100
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_non_thread_channel_skipped(self):
        client = MagicMock()
        text_channel = MagicMock(spec=discord.TextChannel)
        client.get_channel = MagicMock(return_value=text_channel)

        result = await resolve_channel_mention_contexts(
            client, "<#200>", current_channel_id=999
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_thread_without_permission_skipped(self):
        channel = MagicMock(spec=discord.Thread)
        channel.guild = MagicMock()
        channel.id = 300
        channel.name = "secret"
        member = MagicMock()
        channel.guild.get_member = MagicMock(return_value=member)
        perms = MagicMock()
        perms.read_message_history = False
        perms.view_channel = False
        channel.permissions_for = MagicMock(return_value=perms)

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        requester = MagicMock()
        requester.id = 42

        result = await resolve_channel_mention_contexts(
            client, "<#300>", current_channel_id=999, requester=requester
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_thread_context_returned(self):
        import utils.discord_helpers as dh
        ADMIN_USER_IDS.clear()

        channel = MagicMock(spec=discord.Thread)
        channel.guild = None
        channel.id = 400
        channel.name = "public-thread"

        msg = MagicMock()
        msg.author.id = 1
        msg.author.display_name = "Joe"
        msg.content = "some thread message"
        msg.embeds = []
        msg.attachments = []

        async def history_gen(**kwargs):
            yield msg

        channel.history = MagicMock(return_value=history_gen())

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        result = await resolve_channel_mention_contexts(
            client, "<#400>", current_channel_id=999, caller_is_admin=True
        )
        assert "some thread message" in result
        assert "public-thread" in result

    @pytest.mark.asyncio
    async def test_deduplicates_channel_mentions(self):
        client = MagicMock()
        client.get_channel = MagicMock(return_value=None)

        result = await resolve_channel_mention_contexts(
            client, "<#500> <#500> <#500>", current_channel_id=999
        )
        # All return None (channel not found), result should be empty
        assert result == ""

    @pytest.mark.asyncio
    async def test_non_admin_filters_admin_messages_in_thread(self):
        ADMIN_USER_IDS.clear()
        ADMIN_USER_IDS.add(777)

        channel = MagicMock(spec=discord.Thread)
        channel.guild = None
        channel.id = 600
        channel.name = "filtered-thread"

        admin_msg = MagicMock()
        admin_msg.author.id = 777
        admin_msg.author.display_name = "AdminUser"
        admin_msg.content = "top secret admin message"
        admin_msg.embeds = []
        admin_msg.attachments = []

        async def history_gen(**kwargs):
            yield admin_msg

        channel.history = MagicMock(return_value=history_gen())

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        result = await resolve_channel_mention_contexts(
            client, "<#600>", current_channel_id=999, caller_is_admin=False
        )
        assert "top secret admin message" not in result
        ADMIN_USER_IDS.clear()


class TestResolveLinkAttachments:
    """Test attachment handling inside _resolve_one_link."""

    def _make_match(self):
        match = MagicMock()
        match.group = MagicMock(side_effect=lambda i: {
            0: "https://discord.com/channels/1/222/333",
            1: "1", 2: "222", 3: "333"
        }[i])
        return match

    def _make_channel(self):
        channel = MagicMock()
        channel.guild = None
        channel.name = "test-ch"
        return channel

    @pytest.mark.asyncio
    async def test_non_inline_attachment_just_listed(self):
        channel = self._make_channel()
        att = MagicMock()
        att.filename = "image.png"
        att.content_type = "image/png"
        att.size = 100
        att.url = "https://cdn.discordapp.com/image.png"

        msg = MagicMock()
        msg.author.display_name = "U"
        msg.content = "pic"
        msg.embeds = []
        msg.attachments = [att]

        channel.fetch_message = AsyncMock(return_value=msg)
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        _, result = await _resolve_one_link(client, self._make_match(), requester=None, caller_is_admin=True)
        assert "[Attachment: image.png]" in result

    @pytest.mark.asyncio
    async def test_inline_attachment_blocked_by_non_allowed_host(self):
        channel = self._make_channel()
        att = MagicMock()
        att.filename = "data.txt"
        att.content_type = "text/plain"
        att.size = 100
        att.url = "https://evil.com/data.txt"

        msg = MagicMock()
        msg.author.display_name = "U"
        msg.content = ""
        msg.embeds = []
        msg.attachments = [att]

        channel.fetch_message = AsyncMock(return_value=msg)
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        _, result = await _resolve_one_link(client, self._make_match(), requester=None, caller_is_admin=True)
        # Should fall through to bare attachment listing
        assert "[Attachment: data.txt]" in result
        # Content should not be inlined
        assert "evil.com" not in (result or "")

    @pytest.mark.asyncio
    async def test_inline_attachment_downloaded_successfully(self):
        channel = self._make_channel()
        att = MagicMock()
        att.filename = "notes.txt"
        att.content_type = "text/plain"
        att.size = 50
        att.url = "https://cdn.discordapp.com/notes.txt"

        msg = MagicMock()
        msg.author.display_name = "U"
        msg.content = ""
        msg.embeds = []
        msg.attachments = [att]

        channel.fetch_message = AsyncMock(return_value=msg)
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        # Mock aiohttp
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {}

        async def iter_chunked(size):
            yield b"file content here"

        mock_resp.content.iter_chunked = iter_chunked
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_sess = AsyncMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_sess):
            _, result = await _resolve_one_link(client, self._make_match(), requester=None, caller_is_admin=True)

        assert "[Attachment: notes.txt]" in result
        assert "file content here" in result

    @pytest.mark.asyncio
    async def test_inline_attachment_content_length_exceeded(self):
        from utils.discord_helpers import _MAX_LINK_TEXT_BYTES
        channel = self._make_channel()
        att = MagicMock()
        att.filename = "big.txt"
        att.content_type = "text/plain"
        att.size = 50
        att.url = "https://cdn.discordapp.com/big.txt"

        msg = MagicMock()
        msg.author.display_name = "U"
        msg.content = ""
        msg.embeds = []
        msg.attachments = [att]

        channel.fetch_message = AsyncMock(return_value=msg)
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {"Content-Length": str(_MAX_LINK_TEXT_BYTES + 1)}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_sess = AsyncMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_sess):
            _, result = await _resolve_one_link(client, self._make_match(), requester=None, caller_is_admin=True)

        assert "[Attachment: big.txt]" in result

    @pytest.mark.asyncio
    async def test_message_with_embed_included(self):
        channel = self._make_channel()
        embed = MagicMock()
        embed.title = "EmbedTitle"
        embed.description = "EmbedDesc"
        embed.fields = []
        embed.footer = None

        msg = MagicMock()
        msg.author.display_name = "EmbedUser"
        msg.content = ""
        msg.embeds = [embed]
        msg.attachments = []

        channel.fetch_message = AsyncMock(return_value=msg)
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        _, result = await _resolve_one_link(client, self._make_match(), requester=None, caller_is_admin=True)
        assert "EmbedTitle" in result
        assert "EmbedDesc" in result

    @pytest.mark.asyncio
    async def test_fetch_channel_exception_returns_none(self):
        client = MagicMock()
        client.get_channel = MagicMock(return_value=None)
        client.fetch_channel = AsyncMock(side_effect=Exception("network error"))

        match = MagicMock()
        match.group = MagicMock(side_effect=lambda i: {
            0: "https://discord.com/channels/1/222/333",
            1: "1", 2: "222", 3: "333"
        }[i])

        _, result = await _resolve_one_link(client, match, requester=None)
        assert result is None


class TestBuildLayeredLinesExtended:
    def test_layer2_truncated_when_exceeds_max_chars(self):
        from utils.discord_helpers import _build_layered_lines, LAYER1_COUNT, LAYER2_COUNT, MAX_CONTEXT_CHARS
        # Create many messages that together exceed MAX_CONTEXT_CHARS
        big_body = "z" * 2000
        total = LAYER1_COUNT + LAYER2_COUNT
        messages = [(f"user{i}", big_body) for i in range(total)]
        lines, total_chars = _build_layered_lines(messages)
        # Should hit truncation path
        assert total_chars <= MAX_CONTEXT_CHARS or any("truncated" in l for l in lines)

    def test_layer1_truncated_when_exceeds_max_chars(self):
        from utils.discord_helpers import _build_layered_lines, LAYER1_COUNT, MAX_CONTEXT_CHARS
        # Only layer1 messages, each very large
        big_body = "a" * (MAX_CONTEXT_CHARS // 2)
        messages = [(f"user{i}", big_body) for i in range(LAYER1_COUNT)]
        lines, total_chars = _build_layered_lines(messages)
        assert any("truncated" in l for l in lines)

    def test_empty_body_skipped(self):
        from utils.discord_helpers import _build_layered_lines
        messages = [("user1", ""), ("user2", "real content"), ("user3", "")]
        lines, _ = _build_layered_lines(messages)
        assert len(lines) == 1
        assert "real content" in lines[0]

    def test_layer3_messages_skipped(self):
        from utils.discord_helpers import _build_layered_lines, LAYER1_COUNT, LAYER2_COUNT
        # Messages beyond L1+L2 total go to layer3 (index < layer2_start) → skipped
        total = LAYER1_COUNT + LAYER2_COUNT + 5
        messages = [(f"user{i}", f"msg{i}") for i in range(total)]
        lines, _ = _build_layered_lines(messages)
        # Layer3 messages (first 5) should NOT appear
        for line in lines:
            # msg0..msg4 are layer3, should be absent
            assert "msg0" not in line or "msg0" not in lines[0]


class TestAfterMsgsPermissionFilter:
    """H9: after_msgs loop must apply _requester_can_read, same as before_msgs."""

    @pytest.mark.asyncio
    async def test_after_msgs_filters_unauthorized(self):
        import discord
        from unittest.mock import patch as _patch
        from utils.discord_helpers import _resolve_one_link

        # Build a fake requester
        requester = MagicMock()
        requester.id = 42

        # Build a fake thread channel
        channel = MagicMock(spec=discord.Thread)
        channel.guild = MagicMock()
        channel.guild.get_member = MagicMock(return_value=MagicMock())

        # permissions_for returns deny for the requester
        bad_perms = MagicMock()
        bad_perms.read_message_history = False
        channel.permissions_for = MagicMock(return_value=bad_perms)
        channel.name = "test-thread"
        channel.id = 222

        # The referenced message
        ref_msg = MagicMock()
        ref_msg.author.display_name = "RefUser"
        ref_msg.content = "ref content"
        ref_msg.embeds = []
        ref_msg.attachments = []
        ref_msg.guild = channel.guild
        ref_msg.channel = channel

        # after_msg: a message that should be filtered out (no read permission)
        after_msg = MagicMock()
        after_msg.author.display_name = "SecretUser"
        after_msg.content = "secret after content"
        after_msg.embeds = []
        after_msg.attachments = []

        async def fake_history(**kwargs):
            after = kwargs.get("after")
            before = kwargs.get("before")
            if after is not None:
                # This is the after_msgs fetch
                yield after_msg
            # before_msgs fetch: yield nothing
            return

        channel.history = MagicMock(side_effect=lambda **kwargs: fake_history(**kwargs))
        channel.fetch_message = AsyncMock(return_value=ref_msg)

        match = MagicMock()
        match.group = MagicMock(side_effect=lambda i: {0: "https://discord.com/channels/1/222/333", 1: "1", 2: "222", 3: "333"}[i])

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        _, result = await _resolve_one_link(client, match, requester=requester, caller_is_admin=False)

        # The after_msg content must not appear in the resolved context
        if result is not None:
            assert "secret after content" not in result

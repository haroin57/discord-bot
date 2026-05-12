"""Coverage improvement tests for utils/discord_helpers, utils/attachments,
services/claude_api, and services/claude_sdk.

Targets uncovered lines identified by pytest-cov.
"""

import asyncio
import json
import os
import tempfile
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Async iterator helper for mocking discord.py async iterators
# ---------------------------------------------------------------------------
class AsyncIteratorMock:
    """Wraps a list of items into an async iterator (async for compatible)."""

    def __init__(self, items):
        self.items = list(items)
        self._iter = None

    def __aiter__(self):
        self._iter = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _make_discord_message(display_name="User", content="hello", embeds=None, attachments=None):
    msg = MagicMock()
    msg.author.display_name = display_name
    msg.content = content
    msg.embeds = embeds or []
    msg.attachments = attachments or []
    return msg


# ===================================================================
# 1. utils/discord_helpers.py
# ===================================================================


class TestFetchThreadContext:
    """Cover lines 82-107 of discord_helpers.py."""

    @pytest.mark.asyncio
    async def test_empty_thread_returns_empty(self):
        from utils.discord_helpers import fetch_thread_context

        thread = MagicMock()
        thread.name = "test-thread"
        thread.history.return_value = AsyncIteratorMock([])
        before_msg = MagicMock()

        result = await fetch_thread_context(thread, before_msg)
        assert result == ""

    @pytest.mark.asyncio
    async def test_thread_with_messages(self):
        from utils.discord_helpers import fetch_thread_context

        msg1 = _make_discord_message("Alice", "Hello there")
        msg2 = _make_discord_message("Bob", "Hi Alice")

        thread = MagicMock()
        thread.name = "general-chat"
        thread.history.return_value = AsyncIteratorMock([msg1, msg2])
        before_msg = MagicMock()

        result = await fetch_thread_context(thread, before_msg)
        assert "[Thread: general-chat" in result
        assert "past 2 messages]" in result
        assert "Alice: Hello there" in result
        assert "Bob: Hi Alice" in result
        assert "[End of thread history]" in result

    @pytest.mark.asyncio
    async def test_thread_with_embeds_and_attachments(self):
        from utils.discord_helpers import fetch_thread_context

        embed = MagicMock()
        embed.title = "Embed Title"
        embed.description = "Embed Desc"
        embed.fields = []
        embed.footer = None

        att = MagicMock()
        att.filename = "image.png"

        msg1 = _make_discord_message("User1", "Check this", embeds=[embed], attachments=[att])

        thread = MagicMock()
        thread.name = "media-thread"
        thread.history.return_value = AsyncIteratorMock([msg1])
        before_msg = MagicMock()

        result = await fetch_thread_context(thread, before_msg)
        assert "**Embed Title**" in result
        assert "Embed Desc" in result
        assert "[Attachment: image.png]" in result

    @pytest.mark.asyncio
    async def test_thread_truncation(self, monkeypatch):
        from utils import discord_helpers
        from utils.discord_helpers import fetch_thread_context

        monkeypatch.setattr(discord_helpers, "MAX_CONTEXT_CHARS", 50)

        msg1 = _make_discord_message("A", "x" * 40)
        msg2 = _make_discord_message("B", "y" * 40)

        thread = MagicMock()
        thread.name = "long-thread"
        thread.history.return_value = AsyncIteratorMock([msg1, msg2])
        before_msg = MagicMock()

        result = await fetch_thread_context(thread, before_msg)
        assert "(...truncated...)" in result

    @pytest.mark.asyncio
    async def test_messages_with_empty_body_skipped(self):
        from utils.discord_helpers import fetch_thread_context

        msg_empty = _make_discord_message("Ghost", "")
        msg_empty.content = ""
        msg_normal = _make_discord_message("User", "actual content")

        thread = MagicMock()
        thread.name = "sparse"
        thread.history.return_value = AsyncIteratorMock([msg_empty, msg_normal])
        before_msg = MagicMock()

        result = await fetch_thread_context(thread, before_msg)
        # The empty message body is stripped, so "Ghost:" should not appear
        assert "Ghost:" not in result
        assert "User: actual content" in result

    @pytest.mark.asyncio
    async def test_messages_with_only_whitespace_content_skipped(self):
        from utils.discord_helpers import fetch_thread_context

        msg_ws = _make_discord_message("Spacer", "   ")
        msg_ok = _make_discord_message("Writer", "Real text")

        thread = MagicMock()
        thread.name = "ws-thread"
        thread.history.return_value = AsyncIteratorMock([msg_ws, msg_ok])
        before_msg = MagicMock()

        result = await fetch_thread_context(thread, before_msg)
        assert "Spacer:" not in result
        assert "Writer: Real text" in result


class TestFetchChannelContext:
    """Cover lines 115-141 of discord_helpers.py."""

    @pytest.mark.asyncio
    async def test_empty_channel_returns_empty(self):
        from utils.discord_helpers import fetch_channel_context

        channel = MagicMock()
        channel.history.return_value = AsyncIteratorMock([])
        before_msg = MagicMock()

        result = await fetch_channel_context(channel, before_msg)
        assert result == ""

    @pytest.mark.asyncio
    async def test_channel_with_messages(self):
        from utils.discord_helpers import fetch_channel_context

        msg1 = _make_discord_message("Alice", "First")
        msg2 = _make_discord_message("Bob", "Second")

        channel = MagicMock()
        channel.history.return_value = AsyncIteratorMock([msg1, msg2])
        before_msg = MagicMock()

        result = await fetch_channel_context(channel, before_msg)
        assert "[Channel recent 2 messages]" in result
        # Channel context reverses the order
        assert "Alice: First" in result
        assert "Bob: Second" in result
        assert "[End of channel history]" in result

    @pytest.mark.asyncio
    async def test_channel_truncation(self, monkeypatch):
        from utils import discord_helpers
        from utils.discord_helpers import fetch_channel_context

        monkeypatch.setattr(discord_helpers, "MAX_CONTEXT_CHARS", 30)

        msg1 = _make_discord_message("A", "x" * 25)
        msg2 = _make_discord_message("B", "y" * 25)

        channel = MagicMock()
        channel.history.return_value = AsyncIteratorMock([msg1, msg2])
        before_msg = MagicMock()

        result = await fetch_channel_context(channel, before_msg)
        assert "(...truncated...)" in result

    @pytest.mark.asyncio
    async def test_channel_with_embeds_and_attachments(self):
        from utils.discord_helpers import fetch_channel_context

        embed = MagicMock()
        embed.title = "News"
        embed.description = "Breaking"
        embed.fields = []
        embed.footer = None

        att = MagicMock()
        att.filename = "doc.pdf"

        msg = _make_discord_message("Reporter", "Update", embeds=[embed], attachments=[att])

        channel = MagicMock()
        channel.history.return_value = AsyncIteratorMock([msg])
        before_msg = MagicMock()

        result = await fetch_channel_context(channel, before_msg)
        assert "**News**" in result
        assert "[Attachment: doc.pdf]" in result


class TestResolveOneLink:
    """Cover lines 162-164, 166 of discord_helpers.py (_resolve_one_link internals)."""

    @pytest.mark.asyncio
    async def test_resolves_link_with_embeds(self):
        from utils.discord_helpers import resolve_message_links

        embed = MagicMock()
        embed.title = "Embedded"
        embed.description = "Info"
        embed.fields = []
        embed.footer = None

        mock_msg = MagicMock()
        mock_msg.author.display_name = "Author"
        mock_msg.content = "Main text"
        mock_msg.embeds = [embed]
        mock_msg.attachments = []

        mock_channel = MagicMock()
        mock_channel.name = "channel"
        # guild=None => treated as DM by the auth guard. We pass requester=None
        # below so the auth check is skipped entirely.
        mock_channel.guild = None
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)

        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=mock_channel)

        text = "See https://discord.com/channels/111/222/333"
        result = await resolve_message_links(mock_client, text, requester=None)
        assert "**Embedded**" in result
        assert "Info" in result

    @pytest.mark.asyncio
    async def test_resolves_link_with_attachments(self):
        from utils.discord_helpers import resolve_message_links

        att = MagicMock()
        att.filename = "photo.jpg"
        att.url = "https://cdn.discordapp.com/photo.jpg"

        mock_msg = MagicMock()
        mock_msg.author.display_name = "Uploader"
        mock_msg.content = ""
        mock_msg.embeds = []
        mock_msg.attachments = [att]

        mock_channel = MagicMock()
        mock_channel.name = "uploads"
        mock_channel.guild = None
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)

        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=mock_channel)

        text = "Link: https://discord.com/channels/1/2/3"
        result = await resolve_message_links(mock_client, text, requester=None)
        assert "[Attachment: photo.jpg" in result
        assert "Uploader" in result

    @pytest.mark.asyncio
    async def test_multiple_links_resolved(self):
        from utils.discord_helpers import resolve_message_links

        mock_msg = MagicMock()
        mock_msg.author.display_name = "User"
        mock_msg.content = "Content"
        mock_msg.embeds = []
        mock_msg.attachments = []

        mock_channel = MagicMock()
        mock_channel.name = "ch"
        mock_channel.guild = None
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)

        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=mock_channel)

        text = (
            "https://discord.com/channels/1/2/3 and "
            "https://discord.com/channels/4/5/6"
        )
        result = await resolve_message_links(mock_client, text, requester=None)
        # Both links resolved
        assert result.count("[Referenced message by User") == 2

    @pytest.mark.asyncio
    async def test_max_resolved_links_capped(self, monkeypatch):
        from utils.discord_helpers import resolve_message_links
        import utils.discord_helpers as dh_mod

        monkeypatch.setattr(dh_mod, "MAX_RESOLVED_LINKS", 1)

        mock_msg = MagicMock()
        mock_msg.author.display_name = "U"
        mock_msg.content = "C"
        mock_msg.embeds = []
        mock_msg.attachments = []

        mock_channel = MagicMock()
        mock_channel.name = "ch"
        mock_channel.guild = None
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)

        mock_client = MagicMock()
        mock_client.get_channel = MagicMock(return_value=mock_channel)

        text = (
            "https://discord.com/channels/1/2/3 "
            "https://discord.com/channels/4/5/6"
        )
        result = await resolve_message_links(mock_client, text, requester=None)
        # Only 1 resolved
        assert result.count("[Referenced message by U") == 1


# ===================================================================
# 2. utils/attachments.py
# ===================================================================


class TestDownloadOne:
    """Cover lines 50-62 of attachments.py via download_attachments."""

    def _make_attachment(self, content_type="image/png", size=100,
                         filename="test.png", att_id=42):
        att = MagicMock()
        att.content_type = content_type
        att.size = size
        att.filename = filename
        att.id = att_id
        # Use the real Discord CDN host; download_attachments now refuses
        # URLs whose host isn't in ALLOWED_ATTACHMENT_HOSTS.
        att.url = f"https://cdn.discordapp.com/{att_id}/{filename}"
        return att

    @pytest.mark.asyncio
    async def test_successful_download(self, tmp_attachment_dir):
        from utils.attachments import download_attachments

        att = self._make_attachment()

        chunk_data = b"fake image data"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.content = MagicMock()
        mock_resp.content.iter_chunked = MagicMock(
            return_value=AsyncIteratorMock([chunk_data])
        )

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=mock_ctx)

        result = await download_attachments(session, [att])
        assert len(result) == 1
        assert result[0].endswith(".png")
        # Verify file contents
        with open(result[0], "rb") as f:
            assert f.read() == chunk_data

    @pytest.mark.asyncio
    async def test_http_error_returns_nothing(self, tmp_attachment_dir):
        from utils.attachments import download_attachments

        att = self._make_attachment()

        mock_resp = MagicMock()
        mock_resp.status = 404

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=mock_ctx)

        result = await download_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_exception_during_download(self, tmp_attachment_dir):
        from utils.attachments import download_attachments

        att = self._make_attachment()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("connection error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=mock_ctx)

        result = await download_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_oversized_attachment_skipped(self, tmp_attachment_dir):
        from utils.attachments import download_attachments

        att = self._make_attachment(size=20 * 1024 * 1024)  # 20MB, exceeds limit

        session = MagicMock()
        result = await download_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_extension_defaults_to_png(self, tmp_attachment_dir):
        from utils.attachments import download_attachments

        att = self._make_attachment(filename="noext")

        chunk_data = b"data"
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.content = MagicMock()
        mock_resp.content.iter_chunked = MagicMock(
            return_value=AsyncIteratorMock([chunk_data])
        )

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=mock_ctx)

        result = await download_attachments(session, [att])
        assert len(result) == 1
        assert result[0].endswith(".png")


class TestCleanupStaleAttachmentsNoDir:
    """Cover line 86 of attachments.py: directory does not exist."""

    def test_nonexistent_dir(self, monkeypatch):
        from utils.attachments import cleanup_stale_attachments

        monkeypatch.setattr("utils.attachments.ATTACHMENT_DIR", "/nonexistent_dir_xyz")
        # Should not raise
        cleanup_stale_attachments()

    def test_oserror_during_cleanup(self, tmp_attachment_dir):
        """Cover lines 94-95: OSError during file removal."""
        from utils.attachments import cleanup_stale_attachments

        old_file = tmp_attachment_dir / "locked.png"
        old_file.write_bytes(b"data")
        os.utime(old_file, (0, 0))

        with patch("os.remove", side_effect=OSError("permission denied")):
            # Should not raise despite OSError
            cleanup_stale_attachments()


# ===================================================================
# 3. services/claude_api.py
# ===================================================================


class TestStreamSync:
    """Cover lines 115-153 of claude_api.py."""

    def test_successful_streaming(self, monkeypatch):
        from services.claude_api import _stream_sync

        chunks = []
        mock_stream = MagicMock()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.text_stream = iter(["Hello", " World"])

        mock_client = MagicMock()
        mock_client.beta.messages.stream.return_value = mock_stream

        _stream_sync(mock_client, [{"role": "user", "content": "hi"}], chunks)
        assert "".join(chunks) == "Hello World"

    def test_rate_limit_retry(self, monkeypatch):
        import anthropic
        from services import claude_api as api_mod
        from services.claude_api import _stream_sync

        monkeypatch.setattr(api_mod, "RETRY_BACKOFF_BASE", 0.01)

        call_count = 0

        def mock_stream(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise anthropic.RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429),
                    body=None,
                )
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.text_stream = iter(["Recovered"])
            return ctx

        mock_client = MagicMock()
        mock_client.beta.messages.stream = mock_stream

        chunks: list[str] = []
        _stream_sync(mock_client, [{"role": "user", "content": "test"}], chunks)
        assert call_count == 2
        assert "Recovered" in "".join(chunks)

    def test_rate_limit_exhausted_raises(self, monkeypatch):
        import anthropic
        from services import claude_api as api_mod
        from services.claude_api import _stream_sync

        monkeypatch.setattr(api_mod, "RETRY_BACKOFF_BASE", 0.001)
        monkeypatch.setattr(api_mod, "MAX_RETRIES", 2)

        def mock_stream(**kwargs):
            raise anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429),
                body=None,
            )

        mock_client = MagicMock()
        mock_client.beta.messages.stream = mock_stream

        with pytest.raises(anthropic.RateLimitError):
            _stream_sync(mock_client, [{"role": "user", "content": "x"}], [])

    def test_stall_detection_warning(self, monkeypatch):
        from services.claude_api import _stream_sync

        # We simulate a gap larger than STREAM_STALL_THRESHOLD_S
        # by monkey-patching time.monotonic to jump
        call_times = iter([0.0, 0.0, 35.0, 35.0])  # gap of 35s > 30s threshold

        monkeypatch.setattr("time.monotonic", lambda: next(call_times))

        mock_stream = MagicMock()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.text_stream = iter(["chunk"])

        mock_client = MagicMock()
        mock_client.beta.messages.stream.return_value = mock_stream

        chunks: list[str] = []
        _stream_sync(mock_client, [{"role": "user", "content": "hi"}], chunks)
        assert "chunk" in "".join(chunks)

    def test_idle_timeout_aborts(self, monkeypatch):
        from services.claude_api import _stream_sync

        # Call sequence: last_event_time=0.0, first text now=0.0 (gap=0, appended),
        # second text now=100.0 (gap=100 > 90, break before append)
        call_times = iter([0.0, 0.0, 100.0])

        monkeypatch.setattr("time.monotonic", lambda: next(call_times))

        mock_stream = MagicMock()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.text_stream = iter(["partial", "should_not_appear"])

        mock_client = MagicMock()
        mock_client.beta.messages.stream.return_value = mock_stream

        chunks: list[str] = []
        _stream_sync(mock_client, [{"role": "user", "content": "hi"}], chunks)
        # "partial" was appended (gap=0), "should_not_appear" triggered idle timeout
        assert chunks == ["partial"]


class TestCallDirect:
    """Cover lines 166-209 of claude_api.py."""

    @pytest.mark.asyncio
    async def test_new_channel_with_thread_context(self, monkeypatch):
        from services.claude_api import call_direct

        monkeypatch.setattr("services.claude_api.get_client", lambda: MagicMock())
        monkeypatch.setattr("services.claude_api.append_jsonl", lambda *a: None)
        monkeypatch.setattr("services.claude_api.fix_history_integrity", lambda h: None)
        monkeypatch.setattr("services.claude_api.trim_history", lambda h, m: None)

        async def fake_to_thread(fn, *args):
            fn(*args)

        monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

        def fake_stream(client, history, chunks):
            chunks.append("Response text")

        monkeypatch.setattr("services.claude_api._stream_sync", fake_stream)

        conversation = OrderedDict()
        result = await call_direct("Hello", 999, conversation, conv_context="Thread ctx")

        assert result == "Response text"
        history = conversation[999]
        # History should have: thread context user + ack + user + assistant
        assert any("過去のスレッド履歴" in str(m.get("content", "")) for m in history)

    @pytest.mark.asyncio
    async def test_lru_eviction(self, monkeypatch):
        from services.claude_api import call_direct

        monkeypatch.setattr("services.claude_api.get_client", lambda: MagicMock())
        monkeypatch.setattr("services.claude_api.append_jsonl", lambda *a: None)
        monkeypatch.setattr("services.claude_api.fix_history_integrity", lambda h: None)
        monkeypatch.setattr("services.claude_api.trim_history", lambda h, m: None)
        monkeypatch.setattr("services.claude_api.MAX_HISTORY", 2)

        async def fake_to_thread(fn, *args):
            fn(*args)

        monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

        def fake_stream(client, history, chunks):
            chunks.append("ok")

        monkeypatch.setattr("services.claude_api._stream_sync", fake_stream)

        conversation = OrderedDict()
        await call_direct("a", 1, conversation)
        await call_direct("b", 2, conversation)
        await call_direct("c", 3, conversation)

        # With MAX_HISTORY=2, channel 1 should have been evicted
        assert 1 not in conversation
        assert 2 in conversation
        assert 3 in conversation

    @pytest.mark.asyncio
    async def test_error_handling_pops_user_msg(self, monkeypatch):
        from services.claude_api import call_direct

        monkeypatch.setattr("services.claude_api.get_client", MagicMock(
            side_effect=Exception("API down")
        ))
        monkeypatch.setattr("services.claude_api.append_jsonl", lambda *a: None)
        monkeypatch.setattr("services.claude_api.fix_history_integrity", lambda h: None)
        monkeypatch.setattr("services.claude_api.trim_history", lambda h, m: None)

        conversation = OrderedDict()
        result = await call_direct("fail", 1, conversation)

        assert "API error" in result
        # The user message should have been popped from history
        history = conversation[1]
        assert not any(m.get("role") == "user" and m.get("content") == "fail" for m in history)

    @pytest.mark.asyncio
    async def test_empty_response_returns_fallback(self, monkeypatch):
        from services.claude_api import call_direct

        monkeypatch.setattr("services.claude_api.get_client", lambda: MagicMock())
        monkeypatch.setattr("services.claude_api.append_jsonl", lambda *a: None)
        monkeypatch.setattr("services.claude_api.fix_history_integrity", lambda h: None)
        monkeypatch.setattr("services.claude_api.trim_history", lambda h, m: None)

        async def fake_to_thread(fn, *args):
            fn(*args)

        monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

        def fake_stream(client, history, chunks):
            pass  # no chunks appended

        monkeypatch.setattr("services.claude_api._stream_sync", fake_stream)

        conversation = OrderedDict()
        result = await call_direct("hello", 1, conversation)
        assert result == "(No response from Claude)"

    @pytest.mark.asyncio
    async def test_existing_channel_no_seed(self, monkeypatch):
        from services.claude_api import call_direct

        monkeypatch.setattr("services.claude_api.get_client", lambda: MagicMock())
        monkeypatch.setattr("services.claude_api.append_jsonl", lambda *a: None)
        monkeypatch.setattr("services.claude_api.fix_history_integrity", lambda h: None)
        monkeypatch.setattr("services.claude_api.trim_history", lambda h, m: None)

        async def fake_to_thread(fn, *args):
            fn(*args)

        monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

        def fake_stream(client, history, chunks):
            chunks.append("Reply")

        monkeypatch.setattr("services.claude_api._stream_sync", fake_stream)

        conversation = OrderedDict()
        conversation[1] = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ]
        result = await call_direct("new", 1, conversation, conv_context="ctx")
        assert result == "Reply"
        # Thread context should NOT be seeded for existing channels
        assert not any(
            "過去のスレッド履歴" in str(m.get("content", ""))
            for m in conversation[1]
        )


# ===================================================================
# 4. services/claude_sdk.py
# ===================================================================


class TestClassifyError:
    """Cover _classify_error, _error_message, _is_transient."""

    def test_rate_limit(self):
        from services.claude_sdk import _classify_error, _error_message, _is_transient

        assert _classify_error("rate_limit error", "") == "rate_limit"
        assert _classify_error("", "429 too many requests") == "rate_limit"
        assert _classify_error("RateLimitError occurred", "") == "rate_limit"
        assert _is_transient("rate_limit") is True
        msg = _error_message("rate_limit")
        assert "レート制限" in msg

    def test_overloaded(self):
        from services.claude_sdk import _classify_error, _error_message, _is_transient

        assert _classify_error("server overloaded", "") == "overloaded"
        assert _classify_error("", "529 server_overload") == "overloaded"
        assert _is_transient("overloaded") is True
        msg = _error_message("overloaded")
        assert "混雑" in msg

    def test_auth(self):
        from services.claude_sdk import _classify_error, _error_message, _is_transient

        assert _classify_error("authentication failed", "") == "auth"
        assert _classify_error("401 Unauthorized", "") == "auth"
        assert _classify_error("403 Forbidden", "") == "auth"
        assert _classify_error("invalid api key", "") == "auth"
        assert _classify_error("token revoked", "") == "auth"
        assert _classify_error("not logged in", "") == "auth"
        assert _is_transient("auth") is False
        msg = _error_message("auth")
        assert "認証" in msg

    def test_context_overflow(self):
        from services.claude_sdk import _classify_error, _error_message, _is_transient

        assert _classify_error("message too long", "") == "context_overflow"
        assert _classify_error("exceed context limit", "") == "context_overflow"
        assert _classify_error("max_tokens exceed", "") == "context_overflow"
        assert _is_transient("context_overflow") is False
        msg = _error_message("context_overflow")
        assert "長すぎ" in msg

    def test_unknown(self):
        from services.claude_sdk import _classify_error, _error_message, _is_transient

        assert _classify_error("some random error", "") == "unknown"
        assert _is_transient("unknown") is False
        msg = _error_message("unknown")
        assert "内部エラー" in msg


class TestSessionHelpers:
    """Cover _is_session_fresh, _record_session_time, _clear_session_time."""

    def test_fresh_session(self, monkeypatch):
        from services.claude_sdk import (
            _is_session_fresh,
            _record_session_time,
            _session_timestamps,
        )

        _session_timestamps.clear()
        _record_session_time(100)
        assert _is_session_fresh(100) is True

    def test_stale_session(self, monkeypatch):
        from services.claude_sdk import (
            SESSION_TTL_S,
            _is_session_fresh,
            _session_timestamps,
        )

        _session_timestamps.clear()
        # Set timestamp far in the past
        _session_timestamps[200] = time.monotonic() - SESSION_TTL_S - 10
        assert _is_session_fresh(200) is False

    def test_unknown_channel_not_fresh(self):
        from services.claude_sdk import _is_session_fresh, _session_timestamps

        _session_timestamps.clear()
        assert _is_session_fresh(999) is False

    def test_clear_session_time(self):
        from services.claude_sdk import (
            _clear_session_time,
            _record_session_time,
            _session_timestamps,
        )

        _session_timestamps.clear()
        _record_session_time(300)
        assert 300 in _session_timestamps
        _clear_session_time(300)
        assert 300 not in _session_timestamps

    def test_clear_nonexistent_session(self):
        from services.claude_sdk import _clear_session_time, _session_timestamps

        _session_timestamps.clear()
        # Should not raise
        _clear_session_time(999)


class TestStderrHelpers:
    """Cover _read_stderr_file and _cleanup_stderr."""

    def test_read_valid_file(self, tmp_path):
        from services.claude_sdk import _read_stderr_file

        f = tmp_path / "stderr.txt"
        f.write_text("error output here")
        result = _read_stderr_file(str(f))
        assert result == "error output here"

    def test_read_empty_path(self):
        from services.claude_sdk import _read_stderr_file

        assert _read_stderr_file("") == ""
        assert _read_stderr_file(None) == ""

    def test_read_nonexistent_file(self):
        from services.claude_sdk import _read_stderr_file

        assert _read_stderr_file("/nonexistent_xyz") == ""

    def test_read_large_file_truncated(self, tmp_path):
        from services.claude_sdk import _read_stderr_file

        f = tmp_path / "big.txt"
        f.write_text("x" * 20000)
        result = _read_stderr_file(str(f))
        assert len(result) == 10000

    def test_cleanup_with_file(self, tmp_path):
        from services.claude_sdk import _cleanup_stderr

        f = tmp_path / "to_clean.txt"
        f.write_text("temp")
        fileobj = open(f, "r")
        _cleanup_stderr(fileobj, str(f))
        assert not f.exists()
        assert fileobj.closed

    def test_cleanup_without_file(self):
        from services.claude_sdk import _cleanup_stderr

        # Should not raise
        _cleanup_stderr(None, None)
        _cleanup_stderr(None, "/nonexistent_xyz")

    def test_cleanup_already_closed_fileobj(self, tmp_path):
        from services.claude_sdk import _cleanup_stderr

        f = tmp_path / "already_closed.txt"
        f.write_text("data")
        fileobj = open(f, "r")
        fileobj.close()
        _cleanup_stderr(fileobj, str(f))
        assert not f.exists()


class TestCallWithToolsErrorPaths:
    """Cover error/retry paths in call_with_tools."""

    @pytest.mark.asyncio
    async def test_timeout_with_resume_clears_and_retries(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(sdk_mod, "BASE_BACKOFF_S", 0.001)
        monkeypatch.setattr(sdk_mod, "TIMEOUT", 1)

        call_count = 0

        async def mock_query(prompt, options):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError()
            # Second call succeeds
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "recovered"
            msg.session_id = "new-sess"
            msg.total_cost_usd = 0.01
            msg.usage = {"input_tokens": 100, "output_tokens": 50}
            msg.num_turns = 1
            msg.duration_api_ms = 1000
            msg.is_error = False
            yield msg

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        from services.session import SessionInfo
        sessions = {1: SessionInfo(session_id="old-session", mcp_server_keys=["context7", "scholarly"])}
        sdk_mod._session_timestamps[1] = time.monotonic()  # make it fresh

        text, info = await sdk_mod.call_with_tools(
            "hello", 1, sessions, raw_user_text="hello"
        )
        assert text == "recovered"
        assert call_count == 2
        # Old session should be cleared
        assert 1 not in sessions

        # Cleanup
        sdk_mod._session_timestamps.pop(1, None)

    @pytest.mark.asyncio
    async def test_timeout_without_resume_returns_message(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(sdk_mod, "TIMEOUT", 1)

        async def mock_query(prompt, options):
            raise TimeoutError()
            yield  # noqa: unreachable

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        text, sid = await sdk_mod.call_with_tools(
            "hello", 1, {}, raw_user_text="hello"
        )
        assert "タイムアウト" in text
        assert sid is None

    @pytest.mark.asyncio
    async def test_auth_error_returns_immediately(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(sdk_mod, "BASE_BACKOFF_S", 0.001)

        async def mock_query(prompt, options):
            raise Exception("authentication failed: invalid api key")
            yield  # noqa: unreachable

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        text, sid = await sdk_mod.call_with_tools(
            "hello", 1, {}, raw_user_text="hello"
        )
        assert "認証" in text
        assert sid is None

    @pytest.mark.asyncio
    async def test_context_overflow_returns_immediately(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(sdk_mod, "BASE_BACKOFF_S", 0.001)

        async def mock_query(prompt, options):
            raise Exception("message too long: exceed context limit")
            yield  # noqa: unreachable

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        text, sid = await sdk_mod.call_with_tools(
            "hello", 1, {}, raw_user_text="hello"
        )
        assert "長すぎ" in text
        assert sid is None

    @pytest.mark.asyncio
    async def test_transient_error_extends_retries(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(sdk_mod, "BASE_BACKOFF_S", 0.001)
        monkeypatch.setattr(sdk_mod, "MAX_BACKOFF_S", 0.01)
        monkeypatch.setattr(sdk_mod, "DEFAULT_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(sdk_mod, "TRANSIENT_MAX_ATTEMPTS", 3)

        call_count = 0

        async def mock_query(prompt, options):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("rate_limit: 429 too many requests")
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "finally"
            msg.session_id = None
            msg.total_cost_usd = 0.01
            msg.usage = {"input_tokens": 100, "output_tokens": 50}
            msg.num_turns = 1
            msg.duration_api_ms = 1000
            msg.is_error = False
            yield msg

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        text, info = await sdk_mod.call_with_tools(
            "hello", 1, {}, raw_user_text="hello"
        )
        assert text == "finally"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_response_returns_fallback(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))

        async def mock_query(prompt, options):
            # Yield nothing -- empty generator
            return
            yield  # noqa: unreachable

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        text, sid = await sdk_mod.call_with_tools(
            "hello", 1, {}, raw_user_text="hello"
        )
        assert "No response" in text

    @pytest.mark.asyncio
    async def test_status_callback_on_retry(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(sdk_mod, "BASE_BACKOFF_S", 0.001)
        monkeypatch.setattr(sdk_mod, "MAX_BACKOFF_S", 0.01)

        call_count = 0

        async def mock_query(prompt, options):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("rate_limit error")
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "ok"
            msg.session_id = None
            yield msg

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        callback = AsyncMock()
        text, sid = await sdk_mod.call_with_tools(
            "hello", 1, {}, status_callback=callback, raw_user_text="hello"
        )
        assert text == "ok"
        callback.assert_called()
        # Check callback was called with retry message
        retry_calls = [
            c for c in callback.call_args_list
            if "リトライ" in str(c)
        ]
        assert len(retry_calls) > 0

    @pytest.mark.asyncio
    async def test_unknown_error_exhausts_retries(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(sdk_mod, "BASE_BACKOFF_S", 0.001)
        monkeypatch.setattr(sdk_mod, "MAX_BACKOFF_S", 0.01)
        monkeypatch.setattr(sdk_mod, "DEFAULT_MAX_ATTEMPTS", 2)

        async def mock_query(prompt, options):
            raise Exception("something totally unexpected")
            yield  # noqa: unreachable

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        text, sid = await sdk_mod.call_with_tools(
            "hello", 1, {}, raw_user_text="hello"
        )
        assert "内部エラー" in text
        assert sid is None

    @pytest.mark.asyncio
    async def test_session_recorded_on_success(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))
        sdk_mod._session_timestamps.pop(42, None)

        async def mock_query(prompt, options):
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "done"
            msg.session_id = "new-session-id"
            msg.total_cost_usd = 0.01
            msg.usage = {"input_tokens": 100, "output_tokens": 50}
            msg.num_turns = 1
            msg.duration_api_ms = 1000
            msg.is_error = False
            yield msg

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        sessions = {}
        text, info = await sdk_mod.call_with_tools(
            "hello", 42, sessions, raw_user_text="hello"
        )
        assert text == "done"
        assert info is not None
        assert info.session_id == "new-session-id"
        # Session time should be recorded
        assert 42 in sdk_mod._session_timestamps

        # Cleanup
        sdk_mod._session_timestamps.pop(42, None)

    @pytest.mark.asyncio
    async def test_stale_session_skipped(self, monkeypatch):
        from services import claude_sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "claude_semaphore", asyncio.Semaphore(1))

        # Set a stale timestamp
        sdk_mod._session_timestamps[77] = time.monotonic() - sdk_mod.SESSION_TTL_S - 100

        used_resume = []

        async def mock_query(prompt, options):
            used_resume.append(getattr(options, "resume", None))
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "ok"
            msg.session_id = None
            yield msg

        monkeypatch.setattr(sdk_mod, "query", mock_query)

        sessions = {77: "old-stale-session"}
        text, sid = await sdk_mod.call_with_tools(
            "hello", 77, sessions, raw_user_text="hello"
        )
        assert text == "ok"
        # Resume should NOT have been set since session is stale
        assert used_resume[0] is None

        # Cleanup
        sdk_mod._session_timestamps.pop(77, None)

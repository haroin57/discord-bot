"""Characterization tests for bot.on_message public boundary.

These tests freeze the observable behaviour of the on_message entry point
BEFORE the grand-refactor (P0.5-a/b). They depend only on the public boundary:
  - services.claude_sdk.call_with_tools (via bot.call_with_tools)
  - discord.Message / Channel / send mocks
  - Mem0 search/add functions (via bot.search_memories_smart / bot.add_memories)
  - Attachment HTTP download (via bot.download_attachments)

Internal private functions in bot.py are never monkeypatched here.
If a test breaks after refactoring, the refactor has changed a public behaviour.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import discord
import pytest


# ---------------------------------------------------------------------------
# Shared helpers (self-contained — no imports from test_bot.py)
# ---------------------------------------------------------------------------

BOT_USER_ID = 99900
ADMIN_USER_ID = 12345
CHANNEL_ID = 99999


def _make_message(
    *,
    author_id=ADMIN_USER_ID,
    author_bot=False,
    channel_id=CHANNEL_ID,
    content="かなえ hello",
    is_dm=False,
    mentions=None,
    role_mentions=None,
    attachments=None,
    reference=None,
):
    """Build a minimal mock discord.Message."""
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
    message.remove_reaction = AsyncMock()

    return message


def _make_status_msg():
    """Return an AsyncMock that looks like the status message bot.py creates."""
    status = AsyncMock()
    status.edit = AsyncMock()
    status.delete = AsyncMock()
    return status


# ---------------------------------------------------------------------------
# Common patches applied to every test
# ---------------------------------------------------------------------------

# Values that are patched as simple new= replacements (no mock object returned)
_COMMON_NEW_VALUES = {
    "bot.MEM0_HISTORY_SEARCH_ENABLED": False,
    "bot.MEM0_AUTO_RECORD_ENABLED": False,
    "bot.SPINNER_MODE": "text",
}

# Values that are patched with new_callable=AsyncMock (mock object returned)
_COMMON_ASYNC_PATCHES = [
    "bot.call_with_tools",
    "bot.search_memories_smart",
    "bot.add_memories",
    "bot.download_attachments",
    "bot.download_text_attachments",
    "bot.download_arbitrary_attachments",
    "bot.fetch_channel_context",
    "bot.fetch_thread_context",
    "bot.resolve_message_links",
    "bot.resolve_channel_mention_contexts",
    "bot.get_http_session",
]

# Values that are patched with plain MagicMock
_COMMON_SYNC_PATCHES = [
    "bot.cleanup_attachments",
]


class _PatchStack:
    """Context manager that starts a consistent set of patches and provides mock handles."""

    def __init__(self, overrides_new=None, overrides_async=None, overrides_sync=None):
        self._overrides_new = overrides_new or {}
        self._overrides_async = overrides_async or {}
        self._overrides_sync = overrides_sync or {}
        self._patchers = []
        self.mocks = {}

    def __enter__(self):
        # Simple value replacements (config flags, etc.)
        merged_new = dict(_COMMON_NEW_VALUES)
        merged_new.update(self._overrides_new)
        for target, value in merged_new.items():
            p = patch(target, value)
            p.start()
            self._patchers.append(p)

        # AsyncMock patches
        for target in _COMMON_ASYNC_PATCHES:
            if target in self._overrides_async:
                continue  # override applied below
            p = patch(target, new_callable=AsyncMock)
            m = p.start()
            self._patchers.append(p)
            self.mocks[target] = m

        for target, kwargs in self._overrides_async.items():
            p = patch(target, **kwargs)
            m = p.start()
            self._patchers.append(p)
            self.mocks[target] = m

        # Sync MagicMock patches
        for target in _COMMON_SYNC_PATCHES:
            p = patch(target, new_callable=MagicMock)
            m = p.start()
            self._patchers.append(p)
            self.mocks[target] = m

        for target, kwargs in self._overrides_sync.items():
            p = patch(target, **kwargs)
            m = p.start()
            self._patchers.append(p)
            self.mocks[target] = m

        # Set sensible defaults
        self.mocks["bot.call_with_tools"].return_value = ("hello from kanae", None, False)
        self.mocks["bot.search_memories_smart"].return_value = []
        self.mocks["bot.download_attachments"].return_value = []
        self.mocks["bot.download_text_attachments"].return_value = []
        self.mocks["bot.download_arbitrary_attachments"].return_value = []
        self.mocks["bot.fetch_channel_context"].return_value = ""
        self.mocks["bot.fetch_thread_context"].return_value = ""
        self.mocks["bot.resolve_message_links"].return_value = "hello"
        self.mocks["bot.resolve_channel_mention_contexts"].return_value = ""
        self.mocks["bot.get_http_session"].return_value = MagicMock()

        return self

    def __exit__(self, *args):
        for p in reversed(self._patchers):
            p.stop()


# ---------------------------------------------------------------------------
# Fixture: reset bot module state and patch bot.user for every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bot_state(monkeypatch):
    """Isolate bot.py module-level state and inject a mock bot.user."""
    import bot
    bot.cooldowns.clear()
    bot._channel_semas.clear()

    monkeypatch.setattr("bot.COOLDOWN_SECONDS", 5)
    monkeypatch.setattr("bot.RESTRICTED_COOLDOWN_SECONDS", 60)
    monkeypatch.setattr("bot.ADMIN_USER_IDS", {ADMIN_USER_ID})
    monkeypatch.setattr("bot.BOT_KEYWORDS", ["かなえ"])
    monkeypatch.setattr("bot.BOT_ROLE_IDS", set())
    monkeypatch.setattr("bot.MAX_COOLDOWN_ENTRIES", 1000)
    monkeypatch.setattr("bot._USE_SHARED_RATE_LIMIT", False)
    # Make even short queries trigger Mem0 recall
    monkeypatch.setattr("bot.MEM0_MIN_QUERY_LENGTH", 1)

    mock_user = MagicMock()
    mock_user.id = BOT_USER_ID
    patcher = patch.object(
        type(bot.bot), "user", new_callable=PropertyMock, return_value=mock_user
    )
    patcher.start()
    yield
    patcher.stop()

    bot.cooldowns.clear()
    bot._channel_semas.clear()


async def _dispatch(message):
    """Invoke bot.on_message directly."""
    import bot
    await bot.bot.on_message(message)


# ===========================================================================
# Tests
# ===========================================================================

class TestNormalResponse:
    """Flow 1: admin mention → call_with_tools returns fixed response → sent via Discord."""

    @pytest.mark.asyncio
    async def test_admin_mention_response_text_is_sent(self):
        """Admin mention triggers call_with_tools and the response body is sent to Discord."""
        msg = _make_message(content="かなえ hello", author_id=ADMIN_USER_ID)
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].return_value = ("こんにちは先輩", None, False)
            await _dispatch(msg)

        # Collect all text sent via reply or channel.send
        all_sent = [
            str(c)
            for c in (msg.reply.call_args_list + msg.channel.send.call_args_list)
        ]
        assert any("こんにちは先輩" in s for s in all_sent), (
            "Response text must reach Discord via reply or channel.send"
        )

    @pytest.mark.asyncio
    async def test_no_exception_escapes_on_success(self):
        """No exception propagates from on_message on the happy path."""
        msg = _make_message(content="かなえ hello", author_id=ADMIN_USER_ID)
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        with _PatchStack():
            await _dispatch(msg)  # must not raise


class TestLLMFailure:
    """Flow 2: call_with_tools raises → fallback error message sent, no crash."""

    @pytest.mark.asyncio
    async def test_llm_exception_produces_error_message(self):
        """Exception from call_with_tools results in an error reply, not an uncaught exception."""
        msg = _make_message(content="かなえ hello", author_id=ADMIN_USER_ID)
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].side_effect = RuntimeError("simulated API failure")
            await _dispatch(msg)

        # Error is surfaced via status_msg.edit (P0 error path keeps a short explanation)
        edit_calls = status.edit.call_args_list
        assert edit_calls, "status_msg.edit should be called with an error message"
        error_shown = any("❌" in str(c) for c in edit_calls)
        assert error_shown, "Error indicator '❌' must appear in the error edit"

    @pytest.mark.asyncio
    async def test_llm_exception_does_not_propagate(self):
        """RuntimeError from the LLM layer must never propagate out of on_message."""
        msg = _make_message(content="かなえ hello", author_id=ADMIN_USER_ID)
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].side_effect = RuntimeError("boom")
            try:
                await _dispatch(msg)
            except Exception as exc:
                pytest.fail(f"on_message leaked exception: {exc!r}")


class TestMem0RecallAndRecord:
    """Flow 3: Mem0 memories passed to call_with_tools; add_memories called after a turn."""

    @pytest.mark.asyncio
    async def test_mem0_results_injected_into_prompt(self):
        """Memories returned by search_memories_smart are present in the prompt passed to call_with_tools."""
        msg = _make_message(content="かなえ test", author_id=ADMIN_USER_ID)
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        captured_prompt = {}

        async def _capture(prompt, *args, **kwargs):
            captured_prompt["v"] = prompt
            return ("ok", None, False)

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].side_effect = _capture
            ps.mocks["bot.search_memories_smart"].return_value = [
                {"memory": "先輩は札幌出身", "score": 0.9}
            ]
            await _dispatch(msg)

        assert "v" in captured_prompt, "call_with_tools must have been called"
        assert "先輩は札幌出身" in captured_prompt["v"], (
            "Mem0 memory must be injected into the prompt passed to call_with_tools"
        )

    @pytest.mark.asyncio
    async def test_add_memories_called_when_auto_record_on(self):
        """With MEM0_AUTO_RECORD_ENABLED=True and a batch flush, add_memories fires."""
        msg = _make_message(
            content="かなえ " + "x" * 200,
            author_id=ADMIN_USER_ID,
        )
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        # We need to import BATCH_SIZE from the same location worktree bot.py uses
        from services.mem0.batch import BATCH_SIZE

        with _PatchStack(overrides_new={"bot.MEM0_AUTO_RECORD_ENABLED": True}) as ps:
            ps.mocks["bot.call_with_tools"].return_value = ("long response " * 20, None, False)
            # Patch the batch functions at the module level where bot.py will lazy-import them
            with patch("services.mem0.batch.append_turn", return_value=BATCH_SIZE) as mock_at, \
                 patch("services.mem0.batch.pop_batch", return_value=([], {})), \
                 patch("services.mem0.batch.build_batch_summary", return_value="summary"):
                await _dispatch(msg)
                # Allow background tasks (_track_task) to execute
                await asyncio.sleep(0.15)

        # add_memories should have been invoked via the background _mem0_batch_store task
        assert ps.mocks["bot.add_memories"].called, (
            "add_memories should fire when auto-record triggers a batch flush"
        )


class TestAttachmentHandling:
    """Flow 4: image attachment → download_attachments called → image path in prompt."""

    @pytest.mark.asyncio
    async def test_attachment_download_called_and_path_reaches_prompt(self):
        """Presence of an attachment causes download_attachments to be called and the path injected."""
        att = MagicMock()
        att.filename = "photo.png"
        att.content_type = "image/png"
        att.size = 1024
        att.url = "https://cdn.discordapp.com/fake.png"

        msg = _make_message(
            content="かなえ see this",
            author_id=ADMIN_USER_ID,
            attachments=[att],
        )
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        captured_prompt = {}

        async def _capture(prompt, *args, **kwargs):
            captured_prompt["v"] = prompt
            return ("ok", None, False)

        fake_path = "/tmp/discord_bot_attachments/photo.png"

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].side_effect = _capture
            ps.mocks["bot.download_attachments"].return_value = [fake_path]
            await _dispatch(msg)

        assert ps.mocks["bot.download_attachments"].called, (
            "download_attachments must be called when the message has attachments"
        )
        prompt = captured_prompt.get("v", "")
        assert fake_path in prompt or "attached_images" in prompt, (
            "Downloaded image path must appear in the prompt"
        )


class TestReplyContext:
    """Flow 5: reply-to message → replied-to content injected into prompt."""

    @pytest.mark.asyncio
    async def test_reply_context_in_prompt(self):
        """When a message is a reply, the replied-to content appears in the prompt."""
        ref = MagicMock()
        ref.message_id = 111
        ref.cached_message = None

        ref_msg = AsyncMock()
        ref_msg.author = MagicMock()
        ref_msg.author.id = 55555  # external (non-admin) user
        ref_msg.author.display_name = "SomeUser"
        ref_msg.content = "元のメッセージ内容"
        ref_msg.attachments = []

        msg = _make_message(
            content="かなえ これについて教えて",
            author_id=ADMIN_USER_ID,
            reference=ref,
        )
        msg.channel.fetch_message = AsyncMock(return_value=ref_msg)
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        captured_prompt = {}

        async def _capture(prompt, *args, **kwargs):
            captured_prompt["v"] = prompt
            return ("ok", None, False)

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].side_effect = _capture
            await _dispatch(msg)

        prompt = captured_prompt.get("v", "")
        assert "元のメッセージ内容" in prompt, (
            "Replied-to message content must be included in the prompt"
        )
        assert "replied_to_message" in prompt, (
            "Non-trusted reply must use <replied_to_message> tag in prompt"
        )


class TestConcurrentMessages:
    """Flow 6: two messages on the same channel → both processed, both get replies."""

    @pytest.mark.asyncio
    async def test_concurrent_posts_both_get_responses(self):
        """Two simultaneous on_message calls on the same channel must both complete."""
        # Use a unique channel id to avoid interference from other tests
        cid = 77771

        msg_a = _make_message(content="かなえ first", author_id=ADMIN_USER_ID, channel_id=cid)
        msg_b = _make_message(content="かなえ second", author_id=ADMIN_USER_ID, channel_id=cid)

        status_a = _make_status_msg()
        status_b = _make_status_msg()
        msg_a.reply = AsyncMock(return_value=status_a)
        msg_b.reply = AsyncMock(return_value=status_b)

        # Make b share a's channel so we can observe channel.send on both
        msg_b.channel = msg_a.channel

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].side_effect = [
                ("reply A", None, False),
                ("reply B", None, False),
            ]
            await asyncio.gather(
                _dispatch(msg_a),
                _dispatch(msg_b),
            )

        # call_with_tools must have been called exactly twice
        assert ps.mocks["bot.call_with_tools"].call_count == 2, (
            "call_with_tools should be called once per message even when concurrent"
        )

        # Each message should have received at least one reply
        assert msg_a.reply.called, "First message must receive a reply"
        assert msg_b.reply.called, "Second message must receive a reply"


class TestLongResponseSplit:
    """Flow 7: 2000+ char response → split into multiple messages each ≤ 2000 chars."""

    @pytest.mark.asyncio
    async def test_response_exceeding_2000_chars_is_split(self):
        """A response longer than 2000 characters must be split into ≤2000-char chunks."""
        # 2100 chars, no tables/math → hits the split_message code path
        long_response = "A" * 2100
        msg = _make_message(content="かなえ hello", author_id=ADMIN_USER_ID)
        status = _make_status_msg()
        msg.reply = AsyncMock(return_value=status)

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].return_value = (long_response, None, False)
            await _dispatch(msg)

        # Collect every string sent to Discord (excluding the status message)
        texts = []
        for call in msg.reply.call_args_list:
            args, kwargs = call
            if args and isinstance(args[0], str):
                texts.append(args[0])
        for call in msg.channel.send.call_args_list:
            args, kwargs = call
            if args and isinstance(args[0], str):
                texts.append(args[0])

        # Filter out status/reaction messages
        content_texts = [
            t for t in texts
            if not t.startswith("⏳") and not t.startswith("❌") and not t.startswith("📄")
        ]

        # Each chunk must respect the Discord limit
        for t in content_texts:
            assert len(t) <= 2000, (
                f"A sent chunk exceeds 2000 chars: {len(t)} chars"
            )

        # At least 2 chunks must have been sent (since the response is 2100 chars)
        assert len(content_texts) >= 2, (
            f"2100-char response should produce at least 2 chunks, got {len(content_texts)}: {content_texts!r}"
        )


class TestHTTPExceptionOnSend:
    """Flow 8: channel.send raises HTTPException → no crash, processing ends gracefully."""

    @pytest.mark.asyncio
    async def test_http_exception_on_send_does_not_crash(self):
        """If channel.send / message.reply raises HTTPException, on_message must not propagate it."""
        msg = _make_message(content="かなえ hello", author_id=ADMIN_USER_ID)

        http_exc = discord.HTTPException(MagicMock(status=500), "server error")
        status = _make_status_msg()
        # The first reply() call returns the status mock so the handler can edit it;
        # subsequent reply/send calls raise HTTPException to simulate a broken channel.
        msg.reply = AsyncMock(side_effect=[status, http_exc, http_exc])
        msg.channel.send = AsyncMock(side_effect=http_exc)

        with _PatchStack() as ps:
            ps.mocks["bot.call_with_tools"].return_value = ("response text", None, False)
            try:
                await _dispatch(msg)
            except discord.HTTPException as exc:
                pytest.fail(f"HTTPException must not propagate from on_message: {exc!r}")
            except Exception as exc:
                pytest.fail(f"Unexpected exception from on_message: {exc!r}")

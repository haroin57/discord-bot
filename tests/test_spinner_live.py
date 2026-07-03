"""Reproduce/confirm the spinner animation end-to-end through on_message.

The spinner edits status_msg ~once a second (_animate → _spin_edit) while the
model is generating. This test runs on_message with a call_with_tools that
sleeps, and checks how many times status_msg.edit was awaited DURING that
window — i.e. the shimmer. If the spinner is broken, that count is 0.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from tests.test_bot import _make_message

LONG = "VIストラテジーについて前に話したことを詳しく教えてくださいお願いします"


@pytest.fixture(autouse=True)
def _reset_bot_state(monkeypatch):
    import bot
    from services import rate_limit as _rl
    _rl.reset_for_tests()
    bot._channel_semas.clear()
    monkeypatch.setattr("bot.BOT_ROLE_IDS", set())
    mock_user = MagicMock()
    mock_user.id = 99900
    p = patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=mock_user)
    p.start()
    yield
    p.stop()


@pytest.mark.asyncio
async def test_spinner_edits_status_during_generation(monkeypatch):
    import bot
    monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})

    status_msg = MagicMock()
    status_msg.edit = AsyncMock()
    msg = _make_message(is_dm=True, author_id=12345, content=LONG)
    msg.reply = AsyncMock(return_value=status_msg)

    seen = {}

    async def slow_call(*a, **k):
        # Spinner runs at 1 edit/sec; ~2.5s should yield >=2 frames.
        await asyncio.sleep(2.5)
        seen["during"] = status_msg.edit.await_count
        return ("response text", None, False)

    ctxs = [
        patch("bot.search_memories_smart", new_callable=AsyncMock, return_value=[]),
        patch("bot.strip_mention", return_value=LONG),
        patch("bot.call_with_tools", side_effect=slow_call),
        patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()),
        patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]),
        patch("bot.resolve_message_links", new_callable=AsyncMock, return_value=LONG),
        patch("bot.extract_image_paths", return_value=[]),
        patch("bot.split_message", return_value=["response text"]),
        patch("bot.cleanup_attachments"),
        patch("bot.update_session"),
        patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""),
        patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""),
    ]
    for c in ctxs:
        c.start()
    try:
        await bot.bot.on_message(msg)
    finally:
        for c in reversed(ctxs):
            c.stop()

    assert "during" in seen, "call_with_tools was never reached"
    # The spinner must have shimmered status_msg at least once while generating.
    assert seen["during"] >= 1, (
        f"spinner did not edit status_msg during generation "
        f"(edits during call={seen.get('during')})"
    )


@pytest.mark.asyncio
async def test_response_is_fresh_reply_not_status_repurpose(monkeypatch):
    """The streamed response must be a NEW reply, not an in-place edit of the
    spinner's status_msg. Repurposing status_msg made the spinner vanish the
    instant generation started and dropped the reply into the wrong (old)
    timeline position. So: status_msg is deleted at the end, and the response
    text never lands on status_msg via edit."""
    import bot
    monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})

    # First reply == the spinner's status_msg; later replies == response msgs.
    # Distinct objects so we can tell an in-place repurpose from a fresh reply.
    status_msg = MagicMock()
    status_msg.edit = AsyncMock()
    status_msg.delete = AsyncMock()
    reply_n = {"n": 0}

    async def _reply(*a, **k):
        reply_n["n"] += 1
        if reply_n["n"] == 1:
            return status_msg
        m = MagicMock()
        m.edit = AsyncMock()
        m.delete = AsyncMock()
        return m

    msg = _make_message(is_dm=True, author_id=12345, content=LONG)
    msg.reply = AsyncMock(side_effect=_reply)

    RESP = "これは応答本文のテストですよ先輩"

    async def streaming_call(*a, **k):
        cb = k.get("incremental_text_callback")
        await asyncio.sleep(1.2)  # let the spinner animate first
        if cb is not None:
            await cb(RESP)         # stream the response in
        return (RESP, None, False)

    ctxs = [
        patch("bot.search_memories_smart", new_callable=AsyncMock, return_value=[]),
        patch("bot.strip_mention", return_value=LONG),
        patch("bot.call_with_tools", side_effect=streaming_call),
        patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()),
        patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]),
        patch("bot.resolve_message_links", new_callable=AsyncMock, return_value=LONG),
        patch("bot.extract_image_paths", return_value=[]),
        patch("bot.cleanup_attachments"),
        patch("bot.update_session"),
        patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""),
        patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""),
    ]
    for c in ctxs:
        c.start()
    try:
        await bot.bot.on_message(msg)
    finally:
        for c in reversed(ctxs):
            c.stop()

    # The response must NOT have been written onto the spinner's status_msg.
    edit_contents = [
        (c.kwargs.get("content") or "") for c in status_msg.edit.call_args_list
    ]
    assert all(RESP not in c for c in edit_contents), (
        "response text was edited INTO status_msg (in-place repurpose) — "
        "it should be a fresh reply instead"
    )
    # The spinner's status_msg is ephemeral: deleted once the turn finishes.
    assert status_msg.delete.await_count >= 1, (
        "status_msg (spinner) was not deleted — it was repurposed as the response"
    )

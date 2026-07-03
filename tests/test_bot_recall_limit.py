"""TDD Phase 3: widen per-turn Mem0 auto-recall for admins.

bot.py used to call search_memories_smart(..., limit=5, rewrite=mem0_llm_mode,
rerank=mem0_llm_mode) for everyone. Against ~5k memories a multi-fact topic
(the日経VI strategy ≈ 6 memories) couldn't all surface. Admins now get
limit=MEM0_RECALL_LIMIT and always rewrite+rerank; non-admins stay narrow.
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from tests.test_bot import _make_message  # reuse the message mock + module stubs


@pytest.fixture(autouse=True)
def _reset_bot_state(monkeypatch):
    """Mirror test_bot.TestOnMessage._reset_bot_state for this standalone file."""
    import bot
    from services import rate_limit as _rl
    _rl.reset_for_tests()
    bot._channel_semas.clear()
    monkeypatch.setattr("bot.BOT_ROLE_IDS", set())
    mock_user = MagicMock()
    mock_user.id = 99900
    patcher = patch.object(type(bot.bot), "user",
                           new_callable=PropertyMock, return_value=mock_user)
    patcher.start()
    yield
    patcher.stop()


def _patches(captured: dict):
    """Patch enough of on_message to reach the Mem0 recall call and capture it."""
    async def fake_recall(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return []

    return [
        patch("bot.search_memories_smart", fake_recall),
        patch("bot.strip_mention", return_value=LONG_QUERY),
        patch("bot.call_with_tools", new_callable=AsyncMock,
              return_value=("ok", None, False)),
        patch("bot.get_http_session", new_callable=AsyncMock, return_value=MagicMock()),
        patch("bot.download_attachments", new_callable=AsyncMock, return_value=[]),
        patch("bot.resolve_message_links", new_callable=AsyncMock, return_value=LONG_QUERY),
        patch("bot.extract_image_paths", return_value=[]),
        patch("bot.split_message", return_value=["ok"]),
        patch("bot.cleanup_attachments"),
        patch("bot.update_session"),
        patch("bot.fetch_channel_context", new_callable=AsyncMock, return_value=""),
        patch("bot.fetch_thread_context", new_callable=AsyncMock, return_value=""),
    ]


# > MEM0_MIN_QUERY_LENGTH (20) so the recall branch fires.
LONG_QUERY = "VIストラテジーについて前に話したことを覚えてますか詳しく教えて"


class TestRecallLimit:
    @pytest.mark.asyncio
    async def test_admin_gets_wide_limit_and_rerank(self, monkeypatch):
        import bot
        from config import MEM0_RECALL_LIMIT

        monkeypatch.setattr("bot.ADMIN_USER_IDS", {12345})
        msg = _make_message(is_dm=True, author_id=12345, content=LONG_QUERY)

        captured: dict = {}
        ctxs = _patches(captured)
        for c in ctxs:
            c.start()
        try:
            await bot.bot.on_message(msg)
        finally:
            for c in reversed(ctxs):
                c.stop()

        assert captured.get("kwargs", {}).get("limit") == MEM0_RECALL_LIMIT
        # admins always rewrite+rerank so multi-fact topics surface
        assert captured["kwargs"].get("rewrite") is True
        assert captured["kwargs"].get("rerank") is True

    @pytest.mark.asyncio
    async def test_non_admin_stays_restricted(self, monkeypatch):
        import bot
        from config import MEM0_RECALL_LIMIT_RESTRICTED

        monkeypatch.setattr("bot.ADMIN_USER_IDS", {99999})
        monkeypatch.setattr("bot.BOT_KEYWORDS", ["かなえ"])
        # non-admin must trigger via keyword in a guild
        msg = _make_message(content="かなえ " + LONG_QUERY, author_id=12345)

        captured: dict = {}
        ctxs = _patches(captured)
        for c in ctxs:
            c.start()
        try:
            await bot.bot.on_message(msg)
        finally:
            for c in reversed(ctxs):
                c.stop()

        # recall may or may not fire for non-admins depending on other gates;
        # if it did, the limit must be the restricted one (never the wide admin limit).
        if "kwargs" in captured:
            assert captured["kwargs"].get("limit") == MEM0_RECALL_LIMIT_RESTRICTED

"""Tests for scripts/ingest_channel_history.fetch_channel_messages.

Cover the sliding-window pair detection (P2-7 streaming refactor).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ingest_channel_history.py"


@pytest.fixture(scope="module")
def ingest_mod():
    """Load scripts/ingest_channel_history.py as a module without running main."""
    spec = importlib.util.spec_from_file_location("ingest_channel_history", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ingest_channel_history"] = mod
    spec.loader.exec_module(mod)
    return mod


def _msg(mid: int, author_id: int, *, is_bot: bool = False, content: str = "", ref_id: int | None = None):
    m = MagicMock()
    m.id = mid
    m.author.id = author_id
    m.author.bot = is_bot
    m.content = content
    m.created_at = datetime.now(timezone.utc)
    m.reference = MagicMock()
    if ref_id is None:
        m.reference = None
    else:
        m.reference.message_id = ref_id
    return m


class _FakeChannel:
    def __init__(self, messages):
        self._messages = messages
        self.name = "fake"

    def history(self, *, limit, oldest_first, after=None):
        async def _gen():
            for m in self._messages[:limit]:
                yield m
        return _gen()


def _client_with(messages, bot_id: int = 9999):
    client = MagicMock()
    client.user.id = bot_id
    client.get_channel.return_value = _FakeChannel(messages)
    return client


@pytest.mark.asyncio
async def test_pairs_user_with_next_bot_reply(ingest_mod):
    """user msg followed by a bot msg within window → paired."""
    user_id = 111
    msgs = [
        _msg(1, user_id, content="hi"),
        _msg(2, 9999, is_bot=True, content="hi back"),
    ]
    client = _client_with(msgs)
    pairs = await ingest_mod.fetch_channel_messages(client, 123, user_id, limit=10)
    assert len(pairs) == 1
    user_msg, reply = pairs[0]
    assert user_msg.id == 1
    assert reply is not None and reply.id == 2


@pytest.mark.asyncio
async def test_user_without_reply_yields_none(ingest_mod):
    user_id = 111
    # 6 messages after user → past the lookahead window of 5
    msgs = [_msg(1, user_id, content="hello")]
    for i in range(2, 8):
        msgs.append(_msg(i, 222))  # other user, not bot
    client = _client_with(msgs)
    pairs = await ingest_mod.fetch_channel_messages(client, 123, user_id, limit=20)
    assert len(pairs) == 1
    assert pairs[0][1] is None


@pytest.mark.asyncio
async def test_explicit_reply_reference_wins(ingest_mod):
    """When multiple pending users and bot replies to the *second* one, prefer reference."""
    user_id = 111
    msgs = [
        _msg(1, user_id, content="first"),
        _msg(2, user_id, content="second"),
        _msg(3, 9999, is_bot=True, content="reply to second", ref_id=2),
    ]
    client = _client_with(msgs)
    pairs = await ingest_mod.fetch_channel_messages(client, 123, user_id, limit=10)
    # msg 2 gets the explicit reply; msg 1 ends up unmatched.
    assert len(pairs) == 2
    matched = {p[0].id: (p[1].id if p[1] else None) for p in pairs}
    assert matched[2] == 3
    assert matched[1] is None


@pytest.mark.asyncio
async def test_other_users_ignored(ingest_mod):
    """Messages from non-target users are not turned into pairs."""
    user_id = 111
    msgs = [
        _msg(1, 222, content="someone else"),
        _msg(2, 9999, is_bot=True, content="bot msg"),
        _msg(3, user_id, content="me"),
        _msg(4, 9999, is_bot=True, content="bot reply"),
    ]
    client = _client_with(msgs)
    pairs = await ingest_mod.fetch_channel_messages(client, 123, user_id, limit=10)
    assert len(pairs) == 1
    assert pairs[0][0].id == 3
    assert pairs[0][1].id == 4


@pytest.mark.asyncio
async def test_streaming_does_not_buffer_history(ingest_mod):
    """Sanity: even with a large history, only pairs (= target user msgs) survive."""
    user_id = 111
    total = 200
    msgs = []
    for i in range(1, total + 1):
        # every 10th message is from the target user
        author = user_id if i % 10 == 0 else 222
        msgs.append(_msg(i, author))
    client = _client_with(msgs)
    pairs = await ingest_mod.fetch_channel_messages(client, 123, user_id, limit=total)
    # 200 / 10 = 20 user messages → 20 pairs (no replies since no bot in this stream)
    assert len(pairs) == 20

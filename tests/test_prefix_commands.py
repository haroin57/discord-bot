"""Boundary tests for the admin-only ``!purge`` prefix command."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_commands.prefix as prefix_commands


BOT_ID = 100
ADMIN_ID = 200


class _FakeChannel:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.history_calls: list[dict] = []
        self.purge = AsyncMock()
        self.notice = SimpleNamespace(delete=AsyncMock())
        self.send = AsyncMock(return_value=self.notice)

    def history(self, **kwargs):
        self.history_calls.append(kwargs)

        async def _messages():
            for message in self.messages:
                yield message

        return _messages()


def _target(message_id: int):
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=BOT_ID),
        delete=AsyncMock(),
    )


def _command_message(channel: _FakeChannel):
    return SimpleNamespace(
        id=999,
        author=SimpleNamespace(id=ADMIN_ID),
        channel=channel,
        reference=None,
        reply=AsyncMock(),
        delete=AsyncMock(),
    )


def _assert_no_delete_calls(message, channel, targets):
    assert channel.history_calls == []
    channel.purge.assert_not_awaited()
    for target in targets:
        target.delete.assert_not_awaited()
    message.delete.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.parametrize("count", ["0", "-1"])
async def test_non_positive_purge_count_is_rejected_without_delete_calls(count):
    target = _target(1)
    channel = _FakeChannel([target])
    message = _command_message(channel)

    handled = await prefix_commands.handle_prefix_commands(
        message,
        SimpleNamespace(user=SimpleNamespace(id=BOT_ID)),
        bare_text=f"!purge {count}",
        is_dm=False,
        caller_is_admin=True,
    )

    assert handled is True
    assert "1〜200" in message.reply.await_args.args[0]
    _assert_no_delete_calls(message, channel, [target])


@pytest.mark.parametrize(
    "bare_text",
    [
        "!purge",
        "!purge ",
        "!purge abc",
        "!purge --1",
        "!purge 1.0",
        "!purge 0x0",
    ],
)
async def test_invalid_purge_count_is_handled_as_usage_error(bare_text):
    target = _target(1)
    channel = _FakeChannel([target])
    message = _command_message(channel)

    handled = await prefix_commands.handle_prefix_commands(
        message,
        SimpleNamespace(user=SimpleNamespace(id=BOT_ID)),
        bare_text=bare_text,
        is_dm=False,
        caller_is_admin=True,
    )

    assert handled is True
    assert "!purge <N>" in message.reply.await_args.args[0]
    _assert_no_delete_calls(message, channel, [target])


async def test_purge_one_deletes_exactly_one_bot_message(monkeypatch):
    first = _target(1)
    second = _target(2)
    channel = _FakeChannel([first, second])
    message = _command_message(channel)
    sleep = AsyncMock()
    monkeypatch.setattr(prefix_commands.asyncio, "sleep", sleep)

    handled = await prefix_commands.handle_prefix_commands(
        message,
        SimpleNamespace(user=SimpleNamespace(id=BOT_ID)),
        bare_text="!purge 1",
        is_dm=False,
        caller_is_admin=True,
    )

    assert handled is True
    first.delete.assert_awaited_once_with()
    second.delete.assert_not_awaited()
    channel.purge.assert_not_awaited()
    message.delete.assert_awaited_once_with()
    channel.send.assert_awaited_once()
    sleep.assert_awaited_once_with(5)
    channel.notice.delete.assert_awaited_once_with()

"""Tests for the /vscode-sync slash command wired in bot.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest


def _make_interaction(user_id: int):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_choice(value: str):
    c = MagicMock()
    c.value = value
    return c


async def test_non_admin_rejected():
    import bot

    interaction = _make_interaction(user_id=999)
    with patch.object(bot, "ADMIN_USER_IDS", {123}):
        cb = bot.vscode_sync_cmd.callback
        await cb(interaction, _make_choice("on"))

    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "admin" in (args[0] if args else kwargs.get("content", "")).lower()
    assert kwargs.get("ephemeral") is True


async def test_admin_status():
    import bot

    interaction = _make_interaction(user_id=123)
    with patch.object(bot, "ADMIN_USER_IDS", {123}), \
         patch.object(bot.vscode_sync, "is_running", return_value=False):
        cb = bot.vscode_sync_cmd.callback
        await cb(interaction, _make_choice("status"))

    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "stopped" in sent


async def test_admin_on_starts_daemon():
    import bot

    interaction = _make_interaction(user_id=123)
    tracked: list = []

    def fake_track(coro, *, name=None):
        coro.close()
        tracked.append(name)

        class _T:
            def cancel(self): ...
        return _T()

    fake_callback = AsyncMock()
    with patch.object(bot, "ADMIN_USER_IDS", {123}), \
         patch.object(bot.vscode_sync, "is_running", return_value=False), \
         patch.object(bot, "VSCODE_SYNC_THREAD_ID", 999), \
         patch.object(bot, "_track_task", side_effect=fake_track), \
         patch.object(bot, "make_post_callback", return_value=fake_callback):
        cb = bot.vscode_sync_cmd.callback
        await cb(interaction, _make_choice("on"))

    assert tracked == ["vscode_sync"]
    sent = interaction.followup.send.call_args.args[0]
    assert "started" in sent


async def test_admin_on_already_running():
    import bot

    interaction = _make_interaction(user_id=123)
    tracked: list = []

    def fake_track(coro, *, name=None):
        coro.close()
        tracked.append(name)

    with patch.object(bot, "ADMIN_USER_IDS", {123}), \
         patch.object(bot.vscode_sync, "is_running", return_value=True), \
         patch.object(bot, "_track_task", side_effect=fake_track):
        cb = bot.vscode_sync_cmd.callback
        await cb(interaction, _make_choice("on"))

    assert tracked == []
    sent = interaction.followup.send.call_args.args[0]
    assert "already" in sent


async def test_admin_on_missing_thread_id():
    import bot

    interaction = _make_interaction(user_id=123)
    tracked: list = []

    def fake_track(coro, *, name=None):
        coro.close()
        tracked.append(name)

    with patch.object(bot, "ADMIN_USER_IDS", {123}), \
         patch.object(bot.vscode_sync, "is_running", return_value=False), \
         patch.object(bot, "VSCODE_SYNC_THREAD_ID", 0), \
         patch.object(bot, "_track_task", side_effect=fake_track):
        cb = bot.vscode_sync_cmd.callback
        await cb(interaction, _make_choice("on"))

    assert tracked == []
    sent = interaction.followup.send.call_args.args[0]
    assert "未設定" in sent


async def test_admin_off_stops_daemon():
    import bot

    interaction = _make_interaction(user_id=123)
    stop_mock = AsyncMock()

    with patch.object(bot, "ADMIN_USER_IDS", {123}), \
         patch.object(bot.vscode_sync, "stop_daemon", stop_mock):
        cb = bot.vscode_sync_cmd.callback
        await cb(interaction, _make_choice("off"))

    stop_mock.assert_awaited_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "stopped" in sent

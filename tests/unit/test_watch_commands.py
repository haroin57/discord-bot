"""Slash acknowledgements reflect persisted schedules and explicit ownership."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord import app_commands

from bot_commands import watch
from services.work.effects import AdapterCapabilities, AdapterResult, EffectExecutor
from services.work.schedules import ScheduleService


class Adapter:
    capabilities = AdapterCapabilities(("notify",), False, True, False, "message exists", "channel_id")

    async def submit(self, payload, *, idempotency_key):
        return AdapterResult(remote_id="1")

    async def reconcile(self, receipt):
        return None


def fixture(tmp_path, monkeypatch):
    executor = EffectExecutor(tmp_path / "work.sqlite3", clock=lambda: 1000)
    executor.register_adapter("discord", Adapter())
    service = ScheduleService(executor.path, executor, clock=lambda: 1000)
    monkeypatch.setattr(watch, "_service", service)
    monkeypatch.setattr(watch, "_authorize_watch", lambda *_: True)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    watch.register(tree, SimpleNamespace(is_admin=lambda user: user.id == 1))
    interaction = SimpleNamespace(id=1234, user=SimpleNamespace(id=1), channel_id=99,
                                  response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    return service, tree, interaction


async def test_reminder_acknowledges_saved_id_and_does_not_claim_alarm_rang(tmp_path, monkeypatch):
    service, tree, interaction = fixture(tmp_path, monkeypatch)
    await tree.get_command("remind").callback(interaction, "2026-09-10 08:00", "Wake up")
    saved = service.list("1")
    assert len(saved) == 1
    content = interaction.followup.send.call_args.args[0]
    assert saved[0].id in content
    assert "鳴動や既読は確認しません" in content
    assert service.executor.list_effects("1") == []


async def test_same_slash_interaction_does_not_register_two_notifications(tmp_path, monkeypatch):
    service, tree, interaction = fixture(tmp_path, monkeypatch)
    command = tree.get_command("remind")
    await command.callback(interaction, "2026-09-10 08:00", "Wake up")
    await command.callback(interaction, "2026-09-10 08:00", "Wake up")
    assert len(service.list("1")) == 1
    await command.callback(interaction, "2026-09-10 09:00", "Wake up")
    assert len(service.list("1")) == 1
    assert "登録できません" in interaction.followup.send.call_args.args[0]


async def test_replayed_cancelled_registration_does_not_promise_a_new_notification(tmp_path, monkeypatch):
    service, tree, interaction = fixture(tmp_path, monkeypatch)
    command = tree.get_command("remind")
    await command.callback(interaction, "2026-09-10 08:00", "Wake up")
    saved = service.list("1")[0]
    service.cancel(saved.id, "1")
    await command.callback(interaction, "2026-09-10 08:00", "Wake up")
    assert service.list("1") == []
    assert "cancelled" in interaction.followup.send.call_args.args[0]
    assert "投稿を行います" not in interaction.followup.send.call_args.args[0]


async def test_same_watch_interaction_reuses_its_schedule(tmp_path, monkeypatch):
    from services.work.schedules import Observation

    service, tree, interaction = fixture(tmp_path, monkeypatch)
    service.register_observer("work", lambda *_: Observation({"status": "queued"}, "status"))
    command = tree.get_command("watch").get_command("add")
    choice = app_commands.Choice(name="完了した時", value="completed")
    await command.callback(interaction, "work-a", "2026-09-10 08:00", choice)
    await command.callback(interaction, "work-a", "2026-09-10 08:00", choice)
    assert len(service.list("1")) == 1


async def test_failed_registration_does_not_acknowledge_success(tmp_path, monkeypatch):
    service, tree, interaction = fixture(tmp_path, monkeypatch)
    await tree.get_command("remind").callback(interaction, "invalid", "Wake up")
    assert service.list("1") == []
    assert "登録できません" in interaction.followup.send.call_args.args[0]


async def test_non_admin_cannot_register_or_cancel_other_notifications(tmp_path, monkeypatch):
    service, tree, interaction = fixture(tmp_path, monkeypatch)
    interaction.user.id = 2
    await tree.get_command("remind").callback(interaction, "2026-09-10 08:00", "Wake up")
    assert service.list("2") == []
    assert "管理者のみ" in interaction.response.send_message.call_args.args[0]

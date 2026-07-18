"""Characterization tests for Discord command boundary adapters."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot_commands.archive as archive_command
from bot_commands.guards import require_admin_channel
from bot_commands.responses import InteractionResponse
from bot_commands.state import state_from_deps


class _FakeTree:
    def __init__(self) -> None:
        self.commands = {}

    def command(self, *, name, description=None, **_kwargs):
        del description

        def decorator(fn):
            self.commands[name] = fn
            return fn

        return decorator


def _interaction(*, admin_id=123, channel_id=456):
    return SimpleNamespace(
        user=SimpleNamespace(id=admin_id, name="user"),
        channel_id=channel_id,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_response_port_routes_after_defer_to_followup():
    interaction = _interaction()
    response = InteractionResponse(interaction)

    await response.defer(ephemeral=True)
    await response.send("done", ephemeral=True)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.response.send_message.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with("done", ephemeral=True)


async def test_admin_channel_guard_rejects_before_service_work():
    interaction = _interaction()
    response = InteractionResponse(interaction)

    context = await require_admin_channel(
        response,
        interaction,
        lambda _user: False,
    )

    assert context is None
    interaction.response.send_message.assert_awaited_once_with(
        "admin only",
        ephemeral=True,
    )


def test_state_adapter_keeps_injected_mappings_live():
    models = {}
    deps = SimpleNamespace(
        model_overrides=models,
        effort_overrides={},
        provider_overrides={},
        tier_overrides={},
        skill_overrides={},
    )
    state = state_from_deps(deps)

    state.set_model((1, 2), "model-a")

    assert models[(1, 2)] == "model-a"
    assert state_from_deps(deps) is state


async def test_r2recent_defers_before_archive_lookup(monkeypatch):
    interaction = _interaction()

    def recent_images(_channel_id, *, limit):
        assert limit == 5
        assert interaction.response.defer.await_count == 1
        return []

    monkeypatch.setattr(archive_command.r2_archive, "recent_images", recent_images)
    tree = _FakeTree()
    archive_command.register(
        tree,
        SimpleNamespace(is_admin=lambda _user: True),
    )

    await tree.commands["r2recent"](interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert "R2画像記録はありません" in interaction.followup.send.await_args.args[0]

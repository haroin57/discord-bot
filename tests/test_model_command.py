"""Tests for /model — 選択肢化（2026-07-08）後の挙動。

主要4モデル（Fable 5 / Opus 4.8 / Opus 4.7 / Sonnet 5）はワンタップ選択、
選択肢に無い実験用IDは custom 引数、clear で解除 — の3経路を固定する。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord import app_commands

import bot_commands.model_cmds as model_cmds

CHANNEL = 10**15
UID = 573489206240870410


class _FakeTree:
    def __init__(self):
        self.cmds: dict = {}

    def command(self, *, name, description=None, **kw):
        def deco(fn):
            self.cmds[name] = fn
            return fn
        return deco


def _make_interaction(*, channel_id=CHANNEL, uid=UID):
    resp = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    user = SimpleNamespace(id=uid, display_name="先輩", name="haroin")
    return SimpleNamespace(response=resp, user=user, channel_id=channel_id)


def _register(is_admin=True):
    tree = _FakeTree()
    deps = SimpleNamespace(
        is_admin=lambda u: is_admin,
        model_overrides={},
        effort_overrides={},
    )
    model_cmds.register(tree, deps)
    return tree.cmds["model"], deps


def _choice(name, value):
    return app_commands.Choice(name=name, value=value)


class TestModelCommand:
    async def test_non_admin_blocked(self):
        cmd, deps = _register(is_admin=False)
        interaction = _make_interaction()
        await cmd(interaction, model=_choice("Fable 5", "claude-fable-5"))
        interaction.response.send_message.assert_awaited_once_with(
            "admin only", ephemeral=True,
        )
        assert deps.model_overrides == {}

    async def test_choice_sets_override(self):
        cmd, deps = _register()
        interaction = _make_interaction()
        await cmd(interaction, model=_choice("Sonnet 5", "claude-sonnet-5"))
        assert deps.model_overrides[(CHANNEL, UID)] == "claude-sonnet-5"
        args, kwargs = interaction.response.send_message.call_args
        assert "Sonnet 5" in args[0]
        assert kwargs["ephemeral"] is True

    async def test_all_four_primary_models_offered(self):
        # 選択肢の後退防止 pin: 要望された4モデルが常に choices にあること。
        cmd, _ = _register()
        choices = {
            c.value
            for c in cmd.__discord_app_commands_param_choices__["model"]
        } if hasattr(cmd, "__discord_app_commands_param_choices__") else None
        if choices is None:
            import inspect
            src = inspect.getsource(model_cmds)
            for mid in (
                "claude-fable-5", "claude-opus-4-8",
                "claude-opus-4-7", "claude-sonnet-5",
            ):
                assert mid in src
        else:
            assert {
                "claude-fable-5", "claude-opus-4-8",
                "claude-opus-4-7", "claude-sonnet-5",
            } <= choices

    async def test_custom_beats_choice(self):
        cmd, deps = _register()
        interaction = _make_interaction()
        await cmd(
            interaction,
            model=_choice("Fable 5", "claude-fable-5"),
            custom="claude-haiku-4-5-20251001",
        )
        assert deps.model_overrides[(CHANNEL, UID)] == "claude-haiku-4-5-20251001"

    async def test_clear_removes_override(self):
        cmd, deps = _register()
        deps.model_overrides[(CHANNEL, UID)] = "claude-sonnet-5"
        interaction = _make_interaction()
        await cmd(interaction, model=_choice("clear（解除・env既定に戻す）", "clear"))
        assert (CHANNEL, UID) not in deps.model_overrides
        args, _ = interaction.response.send_message.call_args
        assert "解除" in args[0]

    async def test_no_args_shows_usage(self):
        cmd, deps = _register()
        interaction = _make_interaction()
        await cmd(interaction)
        assert deps.model_overrides == {}
        args, _ = interaction.response.send_message.call_args
        assert "選択" in args[0] or "指定" in args[0]

    async def test_outside_channel(self):
        cmd, _ = _register()
        interaction = _make_interaction(channel_id=None)
        await cmd(interaction, model=_choice("Fable 5", "claude-fable-5"))
        args, kwargs = interaction.response.send_message.call_args
        assert "チャンネル外" in args[0]

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord import app_commands

import bot_commands.skills as skills_command
import bot_commands.session_cmds as session_commands
from services.skills import Skill, SkillSelection

CHANNEL = 10**15
USER = 573489206240870410


class _FakeTree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def command(self, *, name: str, description: str | None = None, **_kwargs):
        del description

        def decorator(fn):
            self.commands[name] = fn
            return fn

        return decorator


def _interaction(*, user_id: int = USER, channel_id: int | None = CHANNEL):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, name="user", display_name="user"),
        channel_id=channel_id,
        response=SimpleNamespace(send_message=AsyncMock()),
        original_response=AsyncMock(
            return_value=SimpleNamespace(edit=AsyncMock()),
        ),
    )


def _register(*, admin: bool = True):
    tree = _FakeTree()
    deps = SimpleNamespace(
        is_admin=lambda _user: admin,
        skill_overrides={},
    )
    skills_command.register(tree, deps)
    return tree.commands["skills"], deps


def _choice(value: str) -> app_commands.Choice[str]:
    return app_commands.Choice(name=value, value=value)


def _skills() -> dict[str, Skill]:
    return {
        "inshi-coach": Skill(
            name="inshi-coach",
            description="院試コーチ",
            body="COACH BODY",
            base_dir="/home/test/.claude/skills/inshi-coach",
        ),
        "other": Skill(
            name="other",
            description="other skill",
            body="OTHER",
            base_dir="/home/test/.claude/skills/other",
        ),
    }


async def test_non_admin_is_rejected() -> None:
    command, deps = _register(admin=False)
    interaction = _interaction()

    await command(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "admin only",
        ephemeral=True,
    )
    assert deps.skill_overrides == {}


async def test_load_is_scoped_to_channel_and_user(monkeypatch) -> None:
    command, deps = _register()
    monkeypatch.setattr(
        skills_command,
        "resolve_skill",
        lambda name, invocation: _skills()[name],
    )
    interaction = _interaction()

    await command(interaction, _choice("load"), "inshi-coach")

    assert deps.skill_overrides[(CHANNEL, USER)] == SkillSelection("inshi-coach")
    args = interaction.response.send_message.await_args
    assert "inshi-coach" in args.args[0]
    assert args.kwargs["ephemeral"] is True


async def test_off_clears_only_callers_selection() -> None:
    command, deps = _register()
    deps.skill_overrides[(CHANNEL, USER)] = SkillSelection("inshi-coach")
    deps.skill_overrides[(CHANNEL, USER + 1)] = SkillSelection("other")
    interaction = _interaction()

    await command(interaction, _choice("off"))

    assert (CHANNEL, USER) not in deps.skill_overrides
    assert deps.skill_overrides[(CHANNEL, USER + 1)] == SkillSelection("other")


async def test_default_action_opens_ephemeral_menu(monkeypatch) -> None:
    command, _deps = _register()
    monkeypatch.setattr(skills_command, "user_invocable_skills", lambda **_kw: _skills())
    interaction = _interaction()

    await command(interaction)

    args = interaction.response.send_message.await_args
    assert args.kwargs["ephemeral"] is True
    assert isinstance(args.kwargs["view"], skills_command._SkillsView)
    assert "inshi-coach" in args.args[0]


async def test_reload_forces_shared_registry_refresh(monkeypatch) -> None:
    command, _deps = _register()
    seen: list[bool] = []

    def fake_skills(*, force: bool = False):
        seen.append(force)
        return _skills()

    monkeypatch.setattr(skills_command, "user_invocable_skills", fake_skills)
    interaction = _interaction()

    await command(interaction, _choice("reload"))

    assert seen == [True]
    assert "2件" in interaction.response.send_message.await_args.args[0]


async def test_channel_is_required() -> None:
    command, _deps = _register()
    interaction = _interaction(channel_id=None)

    await command(interaction)

    assert "チャンネル外" in interaction.response.send_message.await_args.args[0]


async def test_clear_command_removes_channel_skill_selections(monkeypatch) -> None:
    tree = _FakeTree()
    overrides = {
        (CHANNEL, USER): SkillSelection("inshi-coach"),
        (CHANNEL, USER + 1): SkillSelection("other"),
        (CHANNEL + 1, USER): SkillSelection("other"),
    }
    deps = SimpleNamespace(
        is_admin=lambda _user: True,
        skill_overrides=overrides,
        provider_overrides={},
        model_overrides={},
    )
    monkeypatch.setattr(
        session_commands,
        "clear_channel_context",
        lambda _channel: {
            "archived_path": None,
            "bytes": 0,
            "sessions_dropped": 0,
        },
    )

    async def fake_clear_codex(_channel):
        return 0

    monkeypatch.setattr(
        "services.codex_app_server.manager.clear_codex_channel",
        fake_clear_codex,
    )
    session_commands.register(tree, deps)
    interaction = _interaction()

    await tree.commands["clear"](interaction)

    assert (CHANNEL, USER) not in overrides
    assert (CHANNEL, USER + 1) not in overrides
    assert overrides[(CHANNEL + 1, USER)] == SkillSelection("other")
    assert "active skill 2 件" in interaction.response.send_message.await_args.args[0]

"""Discord ``/skills`` catalog and conversation-scoped skill loader."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps
from services.skills import (
    Skill,
    SkillError,
    SkillSelection,
    resolve_skill,
    user_invocable_skills,
)

log = logging.getLogger(__name__)

_MAX_SELECT_OPTIONS = 25


def _selection_name(value: object) -> str:
    if isinstance(value, SkillSelection):
        return value.name
    return str(value or "").strip()


def _description(skill: Skill, limit: int = 100) -> str:
    text = skill.description or "No description"
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _SkillSelect(discord.ui.Select):
    def __init__(self, owner: "_SkillsView") -> None:
        self.owner = owner
        visible = list(owner.skills.values())[:_MAX_SELECT_OPTIONS]
        options = [
            discord.SelectOption(
                label=skill.name[:100],
                value=skill.name,
                description=_description(skill),
                default=skill.name == owner.active_name,
            )
            for skill in visible
        ]
        if not options:
            options = [
                discord.SelectOption(
                    label="No user-invocable skills",
                    value="_none",
                )
            ]
        super().__init__(
            placeholder="Select a skill to load",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
            disabled=not visible,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        name = self.values[0]
        if name != "_none":
            try:
                skill = resolve_skill(name, invocation="user")
            except SkillError as exc:
                self.owner.notice = f"Load failed: {exc}"
            else:
                self.owner.overrides[self.owner.key] = SkillSelection(skill.name)
                self.owner.active_name = skill.name
                self.owner.notice = (
                    f"Loaded `{skill.name}`. It applies to subsequent turns "
                    "for this channel and user."
                )
        self.owner.refresh()
        await interaction.response.edit_message(
            content=self.owner.render(),
            view=self.owner,
        )


class _SkillsView(discord.ui.View):
    def __init__(
        self,
        overrides: dict,
        *,
        channel_id: int,
        owner_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.overrides = overrides
        self.key = (channel_id, owner_id)
        self.owner_id = owner_id
        self.active_name = _selection_name(overrides.get(self.key))
        self.notice = ""
        self.skills: dict[str, Skill] = {}
        self.message = None
        self.refresh()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the user who opened this panel can operate it.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def refresh(self, *, force: bool = False) -> None:
        self.skills = dict(sorted(user_invocable_skills(force=force).items()))
        if self.active_name and self.active_name not in self.skills:
            self.overrides.pop(self.key, None)
            self.notice = (
                f"The previously active skill `{self.active_name}` is no "
                "longer user-invocable and was cleared."
            )
            self.active_name = ""
        for item in list(self.children):
            if isinstance(item, _SkillSelect):
                self.remove_item(item)
        self.add_item(_SkillSelect(self))
        self.clear_skill.disabled = not self.active_name

    def render(self) -> str:
        active = f"`{self.active_name}`" if self.active_name else "none"
        lines = [
            "Skills",
            f"Active for this channel and user: {active}",
            "Selecting a skill loads trusted local instructions for subsequent "
            "Claude or Codex turns.",
            "",
        ]
        if self.notice:
            lines.extend((self.notice, ""))
        for skill in self.skills.values():
            marker = " [active]" if skill.name == self.active_name else ""
            lines.append(f"- {skill.name}{marker}: {skill.description}")
        if not self.skills:
            lines.append("- No user-invocable skills are installed.")
        if len(self.skills) > _MAX_SELECT_OPTIONS:
            lines.append(
                f"\nThe menu shows the first {_MAX_SELECT_OPTIONS} skills. "
                "Use the skill argument for another installed name."
            )
        return "\n".join(lines)[:1950]

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.secondary, row=1)
    async def clear_skill(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        previous = self.active_name
        self.overrides.pop(self.key, None)
        self.active_name = ""
        self.notice = (
            f"Cleared `{previous}`. No skill is active for subsequent turns."
            if previous
            else "No skill was active."
        )
        self.refresh()
        await interaction.response.edit_message(
            content=self.render(),
            view=self,
        )

    @discord.ui.button(label="Reload", style=discord.ButtonStyle.primary, row=1)
    async def reload_skills(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.notice = "Reloaded the shared skill directory."
        self.refresh(force=True)
        await interaction.response.edit_message(
            content=self.render(),
            view=self,
        )


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    skill_overrides = getattr(deps, "skill_overrides", None)
    if skill_overrides is None:
        skill_overrides = {}

    async def skill_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not deps.is_admin(interaction.user):
            return []
        needle = (current or "").strip().casefold()
        result: list[app_commands.Choice[str]] = []
        for skill in user_invocable_skills().values():
            if needle and needle not in skill.name.casefold() and needle not in skill.description.casefold():
                continue
            label = skill.name
            if skill.description:
                label += f" — {skill.description}"
            result.append(
                app_commands.Choice(name=_description_text(label, 100), value=skill.name)
            )
            if len(result) >= _MAX_SELECT_OPTIONS:
                break
        return result

    @tree.command(
        name="skills",
        description="ローカルskillの一覧・ロード・解除・再読込 (admin only)",
    )
    @app_commands.describe(
        action="list / load / off / reload",
        skill="loadするskill名。省略時は選択メニューを表示",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="List / menu", value="list"),
        app_commands.Choice(name="Load", value="load"),
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Reload", value="reload"),
    ])
    @app_commands.autocomplete(skill=skill_autocomplete)
    async def skills_cmd(
        interaction: discord.Interaction,
        action: app_commands.Choice[str] | None = None,
        skill: str | None = None,
    ) -> None:
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message(
                "チャンネル外では使えません",
                ephemeral=True,
            )
            return

        selected_action = action.value if action is not None else ""
        if not selected_action:
            selected_action = "load" if (skill or "").strip() else "list"
        key = (channel_id, interaction.user.id)

        if selected_action == "off":
            previous = _selection_name(skill_overrides.pop(key, None))
            message = (
                f"`{previous}`を解除しました。次のターンからskillなしで実行します。"
                if previous
                else "このチャンネルとユーザーではskillはロードされていません。"
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        if selected_action == "reload":
            skills = user_invocable_skills(force=True)
            active = _selection_name(skill_overrides.get(key))
            if active and active not in skills:
                skill_overrides.pop(key, None)
                active = ""
            suffix = f" Active: `{active}`." if active else ""
            await interaction.response.send_message(
                f"共有skillディレクトリを再読込しました。{len(skills)}件を認識しました。{suffix}",
                ephemeral=True,
            )
            return

        if selected_action == "load":
            requested = (skill or "").strip()
            if not requested:
                await interaction.response.send_message(
                    "skillを指定するか、actionをListにして選択メニューを使用してください。",
                    ephemeral=True,
                )
                return
            try:
                resolved = resolve_skill(requested, invocation="user")
            except SkillError as exc:
                await interaction.response.send_message(
                    f"skillをロードできません: {exc}",
                    ephemeral=True,
                )
                return
            skill_overrides[key] = SkillSelection(resolved.name)
            await interaction.response.send_message(
                f"`{resolved.name}`をロードしました。次のターンからこのチャンネルと"
                "ユーザーのClaude/Codex会話へ適用します。`/skills action:Off`で解除できます。",
                ephemeral=True,
            )
            return

        view = _SkillsView(
            skill_overrides,
            channel_id=channel_id,
            owner_id=interaction.user.id,
        )
        await interaction.response.send_message(
            view.render(),
            ephemeral=True,
            view=view,
        )
        try:
            view.message = await interaction.original_response()
        except (discord.HTTPException, AttributeError):
            pass


def _description_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"

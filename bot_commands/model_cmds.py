"""/model /effort — チャンネル×ユーザー単位のモデル/推論強度 override."""

from __future__ import annotations

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    @tree.command(name="model", description="このチャンネル×自分用にモデルを切り替える (admin only)")
    @app_commands.describe(model="例: claude-opus-4-8 / claude-opus-4-7 / claude-fable-5 / clear で解除")
    async def model_cmd(interaction: discord.Interaction, model: str):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        cid = interaction.channel_id
        uid = interaction.user.id
        if cid is None:
            await interaction.response.send_message("チャンネル外では使えません", ephemeral=True)
            return
        key = (cid, uid)
        if model.strip().lower() in ("clear", "reset", "off", "default", ""):
            deps.model_overrides.pop(key, None)
            await interaction.response.send_message(
                "🧹 モデル override を解除しました（env 既定に戻ります）", ephemeral=True,
            )
            return
        deps.model_overrides[key] = model.strip()
        await interaction.response.send_message(
            f"✅ 次のターンから `{model.strip()}` を使います（このチャンネル × あなただけ）",
            ephemeral=True,
        )

    @tree.command(name="effort", description="このチャンネル×自分用に推論強度を切り替える (admin only)")
    @app_commands.describe(level="low / medium / high / xhigh / max / clear")
    @app_commands.choices(level=[
        app_commands.Choice(name="low", value="low"),
        app_commands.Choice(name="medium", value="medium"),
        app_commands.Choice(name="high", value="high"),
        app_commands.Choice(name="xhigh", value="xhigh"),
        app_commands.Choice(name="max", value="max"),
        app_commands.Choice(name="clear", value="clear"),
    ])
    async def effort_cmd(interaction: discord.Interaction, level: app_commands.Choice[str]):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        cid = interaction.channel_id
        uid = interaction.user.id
        if cid is None:
            await interaction.response.send_message("チャンネル外では使えません", ephemeral=True)
            return
        key = (cid, uid)
        if level.value == "clear":
            deps.effort_overrides.pop(key, None)
            await interaction.response.send_message(
                "🧹 effort override を解除しました", ephemeral=True,
            )
            return
        deps.effort_overrides[key] = level.value
        await interaction.response.send_message(
            f"✅ 次のターンから effort=`{level.value}`（このチャンネル × あなただけ）",
            ephemeral=True,
        )

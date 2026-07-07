"""/model /effort — チャンネル×ユーザー単位のモデル/推論強度 override."""

from __future__ import annotations

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    @tree.command(name="model", description="このチャンネル×自分用にモデルを切り替える (admin only)")
    @app_commands.describe(
        model="切替先モデル（clear で解除）",
        custom="選択肢に無いモデルIDを直接指定（model 引数より優先）",
    )
    # 主要モデルは選択肢から1タップで切替（2026-07-08 要望）。ID の手打ちは
    # 選択肢に無い実験用モデルのための custom に残す。
    @app_commands.choices(model=[
        app_commands.Choice(name="Fable 5", value="claude-fable-5"),
        app_commands.Choice(name="Opus 4.8", value="claude-opus-4-8"),
        app_commands.Choice(name="Opus 4.7", value="claude-opus-4-7"),
        app_commands.Choice(name="Sonnet 5", value="claude-sonnet-5"),
        app_commands.Choice(name="clear（解除・env既定に戻す）", value="clear"),
    ])
    async def model_cmd(
        interaction: discord.Interaction,
        model: "app_commands.Choice[str] | None" = None,
        custom: "str | None" = None,
    ):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        cid = interaction.channel_id
        uid = interaction.user.id
        if cid is None:
            await interaction.response.send_message("チャンネル外では使えません", ephemeral=True)
            return
        value = (custom or (model.value if model else "")).strip()
        if not value:
            await interaction.response.send_message(
                "モデルを選択するか、custom にモデルIDを指定してください", ephemeral=True,
            )
            return
        key = (cid, uid)
        if value.lower() in ("clear", "reset", "off", "default"):
            deps.model_overrides.pop(key, None)
            await interaction.response.send_message(
                "🧹 モデル override を解除しました（env 既定に戻ります）", ephemeral=True,
            )
            return
        deps.model_overrides[key] = value
        label = model.name if (model and not custom) else value
        await interaction.response.send_message(
            f"✅ 次のターンから `{label}` を使います（このチャンネル × あなただけ）",
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

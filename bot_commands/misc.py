"""/abort /help — 運用系コマンド."""

from __future__ import annotations

import discord
from discord import app_commands

from services.job_state import cancel_channel_jobs

from bot_commands._deps import CommandDeps


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    @tree.command(name="abort", description="現チャンネルで進行中の応答を中断する (admin only)")
    async def abort_cmd(interaction: discord.Interaction):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return

        channel_id = interaction.channel_id
        cancelled = cancel_channel_jobs(channel_id) if channel_id is not None else 0
        if cancelled == 0:
            await interaction.response.send_message(
                "中断対象なし (進行中・待機中の応答はありません)", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"中断要求を送りました({cancelled}件: 実行中と待機中のジョブを含む)",
            ephemeral=True,
        )

    @tree.command(name="help", description="使えるコマンドの一覧と使い方を表示する")
    async def help_cmd(interaction: discord.Interaction):
        # スラッシュコマンドは tree から動的に列挙するので、コマンドを足しても
        # /help は自動で追従する。prefix コマンドだけ手書きで補う。
        lines = ["**スラッシュコマンド**"]
        for cmd in sorted(tree.get_commands(), key=lambda c: c.name):
            desc = getattr(cmd, "description", "") or ""
            lines.append(f"- `/{cmd.name}` — {desc}")
        lines.append("")
        lines.append("**プレフィックスコマンド（admin、メッセージ本文で）**")
        lines.append("- `!purge <N>` — かなえ自身の直近メッセージを N 件削除")
        lines.append("- `!purge-since HH:MM` — 指定時刻（JST・24h）以降のかなえの投稿を削除")
        lines.append("- `!purge-from` — 起点にしたいメッセージへ**返信**して、それ以降を削除")
        lines.append("- 削除したいメッセージへ**返信**で `!delete` / `削除` / `消して` — その1件を削除")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

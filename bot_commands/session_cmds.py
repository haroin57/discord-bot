"""/compact /clear /context — チャンネル会話コンテキストの操作コマンド."""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands

from services.session import clear_channel_context, get_session

from bot_commands._deps import CommandDeps

log = logging.getLogger(__name__)


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    @tree.command(name="compact", description="このチャンネルの会話履歴を手動で圧縮する (admin only)")
    async def compact_cmd(interaction: discord.Interaction):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル外では使えません", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            # 遅延 import: startup 時の循環 import (bot ← claude_api ← bot) を避ける。
            from services.claude_api.client_factory import make_api_client
            from services.claude_api.compaction import compact_channel_history

            api_client = await make_api_client(restricted=False)
            ok = await compact_channel_history(channel_id, api_client, force=True)
        except Exception as exc:
            await interaction.followup.send(f"圧縮に失敗しました: {exc}", ephemeral=True)
            return
        if ok:
            await interaction.followup.send(
                "✅ 会話履歴を圧縮しました（次ターンから反映されます）", ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "圧縮は実行されませんでした（無効・circuit breaker 開放・対象が小さい等）",
                ephemeral=True,
            )

    @tree.command(
        name="clear",
        description="このチャンネルの会話履歴とセッションをクリアする (admin only)",
    )
    async def clear_cmd(interaction: discord.Interaction):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("チャンネル外では使えません", ephemeral=True)
            return
        try:
            report = clear_channel_context(channel_id)
        except Exception as exc:
            log.exception("/clear failed for ch=%s", channel_id)
            await interaction.response.send_message(
                f"クリアに失敗しました: {exc}", ephemeral=True,
            )
            return
        parts = ["✅ このチャンネルの会話コンテキストをクリアしました。"]
        if report["archived_path"]:
            parts.append(
                f"-# 履歴 {report['bytes']:,} bytes を "
                f"`{os.path.basename(report['archived_path'])}` にアーカイブ済み（監査用に保持）"
            )
        else:
            parts.append("-# 履歴ファイルは無かったのでスキップ")
        parts.append(f"-# in-memory session {report['sessions_dropped']} 件を破棄")
        parts.append("-# 次のメッセージから完全にフレッシュな会話が始まります。")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)

    @tree.command(name="context", description="このチャンネルでの直近トークン使用量と圧縮余裕を表示する (admin only)")
    async def context_cmd(interaction: discord.Interaction):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        cid = interaction.channel_id
        uid = interaction.user.id
        info = get_session(cid, uid) if cid is not None else None
        usage = getattr(info, "last_usage", None) if info else None
        if not usage:
            await interaction.response.send_message(
                "このチャンネル × あなたの組み合わせでは、まだ usage を観測していません",
                ephemeral=True,
            )
            return
        inp = int(usage.get("input_tokens", 0) or 0)
        cr = int(usage.get("cache_read_input_tokens", 0) or 0)
        cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        total_in = inp + cr + cc
        # 500K の圧縮トリガーまでの余裕。env override されてれば追従する。
        from config import HISTORY_COMPACT_TRIGGER_TOKENS
        head = max(0, HISTORY_COMPACT_TRIGGER_TOKENS - total_in)
        msg = (
            f"**直近ターンの usage**\n"
            f"- input: {inp:,}\n"
            f"- cache_read: {cr:,}\n"
            f"- cache_creation: {cc:,}\n"
            f"- output: {out:,}\n"
            f"\n"
            f"**圧縮トリガーまでの余裕**\n"
            f"- 入力側合計: {total_in:,} / 閾値 {HISTORY_COMPACT_TRIGGER_TOKENS:,}\n"
            f"- 残り {head:,} トークン"
        )
        await interaction.response.send_message(msg, ephemeral=True)

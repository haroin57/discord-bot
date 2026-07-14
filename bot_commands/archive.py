"""Admin-only R2 archive discovery, fetch, and state-sync commands."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import discord
from discord import app_commands

import config
from bot_commands._deps import CommandDeps
from services import r2_archive

_DISCORD_FILE_LIMIT = 24 * 1024 * 1024


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    @tree.command(
        name="r2recent",
        description="このチャンネルでR2保存した直近画像を表示する (admin only)",
    )
    @app_commands.describe(count="表示件数（1〜20）")
    async def r2recent_cmd(interaction: discord.Interaction, count: int = 5):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        if interaction.channel_id is None:
            await interaction.response.send_message("チャンネル外では使えません", ephemeral=True)
            return
        count = max(1, min(count, 20))
        rows = await asyncio.to_thread(
            r2_archive.recent_images, interaction.channel_id, limit=count,
        )
        if not rows:
            await interaction.response.send_message(
                "このチャンネルのR2画像記録はありません。", ephemeral=True,
            )
            return
        lines = ["**R2保存済み画像**"]
        for row in rows:
            meta = row.get("metadata") or {}
            name = meta.get("original_name") or Path(row["key"]).name
            status = "送信済み" if row.get("upload_status") == "uploaded" else "送信待ち"
            lines.append(
                f"- `{row.get('created_at', '')}` [{status}] {name} "
                f"({row.get('direction', 'unknown')})\n  `{row['uri']}`"
            )
        await interaction.response.send_message("\n".join(lines)[:1950], ephemeral=True)

    @tree.command(
        name="r2fetch",
        description="R2アーカイブキーを取得してDiscordへ添付する (admin only)",
    )
    @app_commands.describe(key="/r2recentに表示されたr2:// URIまたはオブジェクトキー")
    async def r2fetch_cmd(interaction: discord.Interaction, key: str):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            normalized = r2_archive.normalize_object_key(key)
            original_name = Path(normalized).name
            suffix = Path(original_name.removesuffix(".metadata.json")).suffix[:16]
            os.makedirs(config.ATTACHMENT_DIR, mode=0o700, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix="r2-fetch-", suffix=suffix, dir=config.ATTACHMENT_DIR,
            )
            os.close(fd)
            try:
                path = await asyncio.to_thread(r2_archive.fetch_object, normalized, tmp_name)
                size = path.stat().st_size
                if size > _DISCORD_FILE_LIMIT:
                    await interaction.followup.send(
                        f"R2オブジェクトはDiscord上限を超えます: {size:,} bytes\n"
                        f"CLIで取得してください: `{normalized}`",
                        ephemeral=True,
                    )
                    return
                file = discord.File(path, filename=original_name[:240] or "r2-object")
                try:
                    await interaction.followup.send(
                        f"`r2://{r2_archive.pending_status()['bucket']}/{normalized}`",
                        file=file,
                        ephemeral=True,
                    )
                finally:
                    file.close()
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        except Exception as exc:
            await interaction.followup.send(f"R2取得に失敗しました: {exc}", ephemeral=True)

    @tree.command(
        name="r2sync",
        description=".state差分をR2スプールへ登録して送信する (admin only)",
    )
    async def r2sync_cmd(interaction: discord.Interaction):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            scan = await asyncio.to_thread(r2_archive.sync_state_once)
            flush = await asyncio.to_thread(r2_archive.flush_pending_once)
            await interaction.followup.send(
                "R2同期を実行しました。\n"
                f"- 変更: {scan.get('changed', 0)}件\n"
                f"- スキャンエラー: {len(scan.get('errors', []))}件\n"
                f"- 今回送信: {flush.get('uploaded', 0)}件\n"
                f"- 送信待ち: {flush.get('pending', 0)}件",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"R2同期に失敗しました: {exc}", ephemeral=True)

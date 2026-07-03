"""/abort /research /vscode-sync /help — 運用系コマンド."""

from __future__ import annotations

import discord
from discord import app_commands

from config import (
    DEEP_RESEARCH_ENABLED,
    VSCODE_SYNC_ENABLED,
    VSCODE_SYNC_FLUSH_INTERVAL,
    VSCODE_SYNC_IDLE_NOTIFY_SECONDS,
    VSCODE_SYNC_PROJECTS_ROOT,
    VSCODE_SYNC_THREAD_ID,
)
from services import deep_research, vscode_sync
from services.job_state import cancel_channel_jobs
from services.vscode_sync.discord_post import make_post_callback

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

    @tree.command(
        name="research",
        description="Deep Research: 多段 Web 調査で引用付きレポートを生成 (admin only)",
    )
    @app_commands.describe(
        query="調査したいテーマ",
        skip_clarify="明確化質問をスキップして即実行する",
    )
    async def research_cmd(
        interaction: discord.Interaction,
        query: str,
        skip_clarify: bool = False,
    ):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        if not DEEP_RESEARCH_ENABLED:
            await interaction.response.send_message(
                "Deep Research は現在無効です (DEEP_RESEARCH_ENABLED=false)。", ephemeral=True,
            )
            return
        if not deep_research.has_required_web_backend():
            await interaction.response.send_message(
                "一般 Web 検索バックエンド (brave-search / tavily) が未設定です。"
                ".env に BRAVE_API_KEY か TAVILY_API_KEY を設定してください。",
                ephemeral=True,
            )
            return
        query = (query or "").strip()
        if not query:
            await interaction.response.send_message("query を入力してください。", ephemeral=True)
            return
        channel = interaction.channel
        if channel is None or not hasattr(channel, "create_thread"):
            await interaction.response.send_message(
                "このチャンネルではスレッドを作成できません。サーバーのテキストチャンネルで実行してください。",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        dr_deps = deep_research.DeepResearchDeps(
            bot=deps.bot,
            send_segmented=deps.send_segmented,
            get_http_session=deps.get_http_session,
            track_task=deps.track_task,
        )
        deps.track_task(
            deep_research.run_deep_research(
                channel, interaction.user, query, dr_deps, skip_clarify=skip_clarify,
            ),
            name="deep_research",
        )
        await interaction.followup.send(
            "🔬 Deep Research を開始しました。スレッドで進行します（数分かかることがあります）。",
            ephemeral=True,
        )

    @tree.command(name="vscode-sync", description="VSCode Claude Code → Discord 同期の切替 (admin only)")
    @app_commands.describe(action="on で開始 / off で停止 / status で状態確認")
    @app_commands.choices(action=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="status", value="status"),
    ])
    async def vscode_sync_cmd(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
    ):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if action.value == "status":
            running = vscode_sync.is_running()
            await interaction.followup.send(
                f"vscode_sync: {'running' if running else 'stopped'} "
                f"(enabled={VSCODE_SYNC_ENABLED}, thread={VSCODE_SYNC_THREAD_ID})",
                ephemeral=True,
            )
            return

        if action.value == "on":
            if vscode_sync.is_running():
                await interaction.followup.send("already running", ephemeral=True)
                return
            if VSCODE_SYNC_THREAD_ID <= 0:
                await interaction.followup.send(
                    "VSCODE_SYNC_THREAD_ID が未設定です",
                    ephemeral=True,
                )
                return
            deps.set_vscode_sync_started(True)
            deps.track_task(
                vscode_sync.run_daemon(
                    post_callback=make_post_callback(deps.bot, VSCODE_SYNC_THREAD_ID),
                    projects_root=VSCODE_SYNC_PROJECTS_ROOT,
                    flush_interval=VSCODE_SYNC_FLUSH_INTERVAL,
                    idle_notify_seconds=VSCODE_SYNC_IDLE_NOTIFY_SECONDS,
                ),
                name="vscode_sync",
            )
            await interaction.followup.send("started", ephemeral=True)
            return

        # action.value == "off"
        await vscode_sync.stop_daemon()
        deps.set_vscode_sync_started(False)
        await interaction.followup.send("stopped", ephemeral=True)

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

"""/remember /recall /forget — Mem0 ユーザー制御コマンド (機能3).

自動 ingest に加え、ユーザーが明示的に記憶を登録/閲覧/削除できる口。すべて
発話者本人の Mem0 namespace (user_id = interaction.user.id) のみを対象に
するので、所有権は構造的に担保される (get_all/search は user_id スコープ、
delete は本人の候補からしか選べない)。
"""

from __future__ import annotations

import discord
from discord import app_commands

from services.mem0 import (
    add_memories,
    delete_memory,
    get_all_memories,
    search_memories_smart,
)
from utils.discord_helpers import split_message

from bot_commands._deps import CommandDeps


class _ForgetSelect(discord.ui.Select):
    """削除候補から複数選択させ、選ばれた memory_id を delete_memory する。

    候補は forget_cmd 側で本人 namespace から引いているので、ここで削除できるのは
    必ず発話者本人の記憶に限られる。
    """

    def __init__(self, rows: list[dict]):
        self._by_value: dict[str, tuple[str, str]] = {}
        options: list[discord.SelectOption] = []
        for i, r in enumerate(rows[:25]):
            mid = r.get("id")
            if not mid:
                continue
            text = (r.get("memory") or "").strip()
            self._by_value[str(i)] = (mid, text)
            options.append(
                discord.SelectOption(label=(text[:95] or "(空の記憶)"), value=str(i))
            )
        super().__init__(
            placeholder="削除する記憶を選択（複数可）",
            min_values=1,
            max_values=max(len(options), 1),
            options=options or [discord.SelectOption(label="(候補なし)", value="_none")],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        deleted: list[str] = []
        failed: list[str] = []
        for v in self.values:
            entry = self._by_value.get(v)
            if not entry:
                continue
            mid, text = entry
            ok = await delete_memory(mid)
            (deleted if ok else failed).append(text[:60] or "(空)")
        msg = ""
        if deleted:
            msg += "🗑️ 削除しました:\n" + "\n".join(f"- {t}" for t in deleted)
        if failed:
            msg += ("\n" if msg else "") + "削除失敗:\n" + "\n".join(f"- {t}" for t in failed)
        await interaction.followup.send(msg or "削除対象なし", ephemeral=True)


class _ForgetView(discord.ui.View):
    def __init__(self, rows: list[dict]):
        super().__init__(timeout=120)
        self.add_item(_ForgetSelect(rows))


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    @tree.command(name="remember", description="指定したテキストを長期記憶に登録する (admin only)")
    @app_commands.describe(text="覚えておいてほしい内容")
    async def remember_cmd(interaction: discord.Interaction, text: str):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)
        try:
            result = await add_memories(
                user_text=text, user_id=uid, context_hint="(手動登録 via /remember)",
            )
        except Exception as exc:
            await interaction.followup.send(f"記憶に失敗しました: {exc}", ephemeral=True)
            return
        if not result:
            await interaction.followup.send("記憶システムが利用できませんでした", ephemeral=True)
            return
        if result.get("error"):
            await interaction.followup.send(
                f"記憶に失敗しました: {result['error']}", ephemeral=True,
            )
            return
        kept = result.get("kept", 0)
        if kept > 0:
            await interaction.followup.send(f"✅ {kept}件を記憶しました", ephemeral=True)
        else:
            sk = result.get("skipped", {})
            await interaction.followup.send(
                f"記憶対象として保存されませんでした (重複や低重要度と判定: {sk})",
                ephemeral=True,
            )

    @tree.command(name="recall", description="自分についての記憶を検索・一覧する (admin only)")
    @app_commands.describe(query="検索語 (省略で最近の記憶を一覧)")
    async def recall_cmd(interaction: discord.Interaction, query: str | None = None):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)
        try:
            if query:
                rows = await search_memories_smart(
                    query, uid, limit=10, rewrite=True, rerank=True,
                    current_channel_id=(
                        str(interaction.channel_id) if interaction.channel_id else None
                    ),
                )
            else:
                rows = await get_all_memories(uid, limit=20)
        except Exception as exc:
            await interaction.followup.send(f"検索に失敗しました: {exc}", ephemeral=True)
            return
        if not rows:
            await interaction.followup.send("該当する記憶はありませんでした", ephemeral=True)
            return
        lines = [f"{i}. {(r.get('memory') or '').strip()}" for i, r in enumerate(rows, 1)]
        header = (
            f"🧠 記憶 {len(rows)}件"
            + (f"（検索: {query}）" if query else "（最近の登録）")
            + ":\n"
        )
        for chunk in split_message(header + "\n".join(lines)):
            await interaction.followup.send(chunk, ephemeral=True)

    @tree.command(name="forget", description="自分についての記憶を選んで削除する (admin only)")
    @app_commands.describe(query="削除候補を絞る検索語 (省略で最近の記憶から選択)")
    async def forget_cmd(interaction: discord.Interaction, query: str | None = None):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)
        try:
            if query:
                rows = await search_memories_smart(
                    query, uid, limit=25, rewrite=True, rerank=True,
                    current_channel_id=(
                        str(interaction.channel_id) if interaction.channel_id else None
                    ),
                )
            else:
                rows = await get_all_memories(uid, limit=25)
        except Exception as exc:
            await interaction.followup.send(f"検索に失敗しました: {exc}", ephemeral=True)
            return
        rows = [r for r in rows if r.get("id")]
        if not rows:
            await interaction.followup.send("削除できる記憶がありませんでした", ephemeral=True)
            return
        await interaction.followup.send(
            f"削除する記憶を選んでください（候補 {len(rows[:25])}件、120秒以内）:",
            view=_ForgetView(rows),
            ephemeral=True,
        )

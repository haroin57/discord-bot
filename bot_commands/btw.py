"""/btw — メイン会話を邪魔せず、今の文脈のまま横で軽く質問する (admin only).

CLI 純正の /btw（"Ask a quick side question without interrupting the main
conversation"）の Discord 版。現在のチャンネル文脈をフォークして側問に答え、
Q&A は JSONL トランスクリプトに書き戻さない（replay / compaction を汚さない）。
返信は呼び出した admin だけに ephemeral で返す。実処理は非永続サービス
``services.claude_api.side_question`` に委譲する。
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps

log = logging.getLogger(__name__)

# "+N earlier /btw" 用の軽量カウンタ。プロセス揮発でよい fail-open な command-local
# 状態なので CommandDeps には通さない（_deps.py の方針に従う）。
_btw_counts: dict[int, int] = {}

_CHUNK_LIMIT = 1900  # header/footer 分の余白を残した Discord 2000 字上限
_MAX_CHUNKS = 3      # ephemeral でも spam しないための打ち切り


def _chunk(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """段落 → 行の順に境界を優先して ``limit`` 以下へ分割する。"""
    out: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        piece = para if not buf else f"{buf}\n\n{para}"
        if len(piece) <= limit:
            buf = piece
            continue
        if buf:
            out.append(buf)
            buf = ""
        if len(para) <= limit:
            buf = para
            continue
        # 単一段落が長すぎる → 行単位、それでも溢れれば hard split。
        for line in para.split("\n"):
            while len(line) > limit:
                out.append(line[:limit])
                line = line[limit:]
            piece = line if not buf else f"{buf}\n{line}"
            if len(piece) <= limit:
                buf = piece
            else:
                if buf:
                    out.append(buf)
                buf = line
    if buf:
        out.append(buf)
    return out or [text[:limit]]


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    @tree.command(
        name="btw",
        description="メイン会話を邪魔せず、今の文脈のまま横で軽く質問する (admin only)",
    )
    @app_commands.describe(question="ついでに聞きたいこと（この会話には残りません）")
    async def btw_cmd(interaction: discord.Interaction, question: str):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message(
                "チャンネル外では使えません", ephemeral=True,
            )
            return
        q = question.strip()
        if not q:
            await interaction.response.send_message("質問が空です", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        key = (channel_id, interaction.user.id)
        model = deps.model_overrides.get(key)
        effort = deps.effort_overrides.get(key)
        author = (
            getattr(interaction.user, "display_name", None)
            or interaction.user.name
        )
        try:
            # 遅延 import: startup 時の循環 import を避ける（/compact と同様）。
            from services.claude_api.client_factory import make_api_client
            from services.claude_api.side_question import answer_side_question

            api_client = await make_api_client(restricted=False)
            result = await answer_side_question(
                channel_id,
                q,
                api_client,
                user_id=interaction.user.id,
                model=model,
                effort=effort,
                author=author,
            )
        except Exception as exc:
            log.exception("/btw failed for ch=%s", channel_id)
            await interaction.followup.send(
                f"横質問に失敗しました: {exc}", ephemeral=True,
            )
            return

        _btw_counts[channel_id] = _btw_counts.get(channel_id, 0) + 1
        earlier = _btw_counts[channel_id] - 1

        answer = result.answer.strip()
        if not answer:
            await interaction.followup.send("（応答が空でした）", ephemeral=True)
            return

        chunks = _chunk(answer)
        truncated = len(chunks) > _MAX_CHUNKS
        chunks = chunks[:_MAX_CHUNKS]

        footer = (
            f"-# +{earlier} earlier /btw · "
            f"~{result.input_tokens:,}→{result.output_tokens:,} tok · "
            f"{result.duration_ms / 1000:.1f}s"
        )
        notes: list[str] = []
        if result.paused_reason:
            notes.append("-# ※ max_turns 到達で打ち切り")
        if truncated:
            notes.append("-# 長いので途中まで")

        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            parts: list[str] = []
            if i == 0:
                parts.append("💬 **横から一言**（この会話には残していません）")
            parts.append(chunk)
            if i == last:
                parts.append(footer)
                parts.extend(notes)
            await interaction.followup.send("\n".join(parts), ephemeral=True)

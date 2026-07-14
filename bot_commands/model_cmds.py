"""/provider /model /effort /tier — channel/user scoped runtime overrides."""

from __future__ import annotations

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    # Older tests and external command registrars construct a lightweight
    # dependency object that predates provider routing. Keep that boundary
    # source-compatible while the real bot injects its shared live mapping.
    provider_overrides = getattr(deps, "provider_overrides", None)
    if provider_overrides is None:
        provider_overrides = {}

    @tree.command(
        name="provider",
        description="このチャンネル×自分用のAI providerを切り替える (admin only)",
    )
    @app_commands.describe(provider="Claude / Codex / 既定に戻す")
    @app_commands.choices(provider=[
        app_commands.Choice(name="Codex (ChatGPT OAuth)", value="codex"),
        app_commands.Choice(name="Claude", value="claude"),
        app_commands.Choice(name="clear（env既定に戻す）", value="clear"),
    ])
    async def provider_cmd(
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
    ):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        cid = interaction.channel_id
        uid = interaction.user.id
        if cid is None:
            await interaction.response.send_message(
                "チャンネル外では使えません", ephemeral=True,
            )
            return
        key = (cid, uid)
        if provider.value == "clear":
            provider_overrides.pop(key, None)
            await interaction.response.send_message(
                "provider override を解除しました。model override がある場合は"
                "そのモデルに対応するproviderを使用します。",
                ephemeral=True,
            )
            return
        current_model = str(deps.model_overrides.get(key) or "").lower()
        if (
            provider.value == "codex"
            and current_model.startswith("claude-")
        ) or (
            provider.value == "claude"
            and current_model.startswith(("gpt-", "o1", "o3", "o4"))
        ):
            deps.model_overrides.pop(key, None)
        provider_overrides[key] = provider.value
        await interaction.response.send_message(
            f"次のターンから provider=`{provider.value}` を使用します"
            "（このチャンネル × あなただけ）",
            ephemeral=True,
        )

    @tree.command(name="model", description="このチャンネル×自分用にモデルを切り替える (admin only)")
    @app_commands.describe(
        model="切替先モデル（clear で解除）",
        custom="選択肢に無いモデルIDを直接指定（model 引数より優先）",
    )
    # 主要モデルは選択肢から1タップで切替（2026-07-08 要望）。ID の手打ちは
    # 選択肢に無い実験用モデルのための custom に残す。
    @app_commands.choices(model=[
        app_commands.Choice(name="GPT-5.6 Sol (Codex)", value="gpt-5.6-sol"),
        app_commands.Choice(name="GPT-5.6 Terra (Codex)", value="gpt-5.6-terra"),
        app_commands.Choice(name="GPT-5.6 Luna (Codex)", value="gpt-5.6-luna"),
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
        # A model selection is an explicit provider signal. This prevents a
        # GPT slug from being handed to Anthropic (or a Claude slug to Codex)
        # while still allowing /provider to be used independently.
        if value.lower().startswith(("gpt-", "o1", "o3", "o4")):
            provider_overrides[key] = "codex"
        elif value.lower().startswith("claude-"):
            provider_overrides[key] = "claude"
        label = model.name if (model and not custom) else value
        await interaction.response.send_message(
            f"次のターンから `{label}` を使います（このチャンネル × あなただけ）",
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
        app_commands.Choice(name="ultra (Codex Sol/Terra)", value="ultra"),
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

    @tree.command(
        name="tier",
        description="熟考モードの呼び出し方を制御する (admin only)",
    )
    @app_commands.describe(mode="熟考固定 / 会話固定 / 解除")
    # 2026-07-08: スコアによる自動 deliberate 昇格は既定 OFF になった
    # (config.DELIB_ROUTER_AUTO_ENABLED)。熟考が要るときはこのコマンドで固定
    # するか、メッセージに「熟考」「じっくり」等の明示マーカーを書く。
    @app_commands.choices(mode=[
        app_commands.Choice(name="熟考モード固定（毎ターン深い検証パイプライン）", value="deliberate"),
        app_commands.Choice(name="会話モード固定（明示マーカーも無視して即答）", value="chat"),
        app_commands.Choice(name="解除（既定: 文中の明示マーカーのみで熟考）", value="clear"),
    ])
    async def tier_cmd(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
    ):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("admin only", ephemeral=True)
            return
        cid = interaction.channel_id
        uid = interaction.user.id
        if cid is None:
            await interaction.response.send_message("チャンネル外では使えません", ephemeral=True)
            return
        key = (cid, uid)
        if mode.value == "clear":
            deps.tier_overrides.pop(key, None)
            await interaction.response.send_message(
                "🧹 tier override を解除しました（文中の「熟考」「じっくり」等の"
                "明示マーカーだけで熟考モードに入ります）",
                ephemeral=True,
            )
            return
        deps.tier_overrides[key] = mode.value
        label = "🧠 熟考モード固定" if mode.value == "deliberate" else "💬 会話モード固定"
        await interaction.response.send_message(
            f"✅ {label} にしました（このチャンネル × あなただけ、/tier 解除 で戻せます）",
            ephemeral=True,
        )

"""/commit /review /security-review — free-code prompt slash commands in Discord.

These wire the ported ``free_code_commands`` prompt commands into the bot so they
run with the **same algorithm** free-code uses for an inline prompt slash command
(``getMessagesForPromptSlashCommand``): expand the template (running ``!`cmd``` in
the repo cwd) → build the ``<command-name>`` tags → parse ``allowedTools`` → submit
as a tool-enabled model turn. The algorithm lives in
``services.free_code_bridge``; this module is only the Discord adapter.

Faithful mapping (ADR-0048):
- expansion + tags + allowed-tools parsing → ``build_prompt_command_turn`` (bridge)
- ``shouldQuery: true`` (run model with tools) → ``call_with_tools`` on the channel
  session, serialised by the per-channel semaphore like a normal turn
- admin-only: the bot's admin already has unrestricted tools, satisfying the
  command's ``allowedTools`` widening.

Operational note: the bot's WorkingDirectory is the prod repo, so ``/commit``
commits there (free-code's cwd semantics). ``FREE_CODE_SLASH_CWD`` overrides it.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps

log = logging.getLogger(__name__)

# The subset of free-code prompt commands that have a real Discord equivalent.
# CLI/TUI-only commands (/vim, /theme, /login, ...) are intentionally excluded
# (ADR-0047/0048). Names are the free-code canonical names.
_WIRED_COMMANDS = ("commit", "review", "security-review")

_CHUNK_LIMIT = 1900
_MAX_CHUNKS = 4


def _load_commands() -> dict:
    """Build the {name: PromptCommand} map for the wired subset (lazy import)."""

    from free_code_commands.commands.prompt_commands import (
        commit,
        review,
        security_review,
    )

    return {"commit": commit(), "review": review(), "security-review": security_review()}


def _chunk(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """Split on paragraph then line boundaries into <= ``limit`` pieces."""

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


async def _run(interaction: discord.Interaction, deps: CommandDeps, command, args: str) -> None:
    """Execute one free-code prompt command as a tool-enabled channel turn."""

    if not deps.is_admin(interaction.user):
        await interaction.response.send_message("admin only", ephemeral=True)
        return
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message("チャンネル外では使えません", ephemeral=True)
        return

    # Public defer: the command runs in the main conversation (free-code parity),
    # so its result belongs in the channel, not an ephemeral reply.
    await interaction.response.defer()

    args = (args or "").strip()

    # Steps 1-4: expand + tags + allowedTools via the bridge (the shared algorithm).
    try:
        from services.free_code_bridge import BotCommandRuntime, build_prompt_command_turn

        prepared = await build_prompt_command_turn(command, args, BotCommandRuntime())
    except Exception as exc:
        log.exception("/%s prompt build failed", command.name)
        await interaction.followup.send(f"`/{command.name}` のプロンプト構築に失敗しました: {exc}")
        return

    # Step 5: run the model with tools, on the channel session, serialised by the
    # per-channel semaphore (same gate on_message uses).
    author = getattr(interaction.user, "display_name", None) or interaction.user.name
    key = (channel_id, interaction.user.id)
    model = deps.model_overrides.get(key)
    effort = deps.effort_overrides.get(key)
    active_skill_prompt = ""
    skill_selection = getattr(deps, "skill_overrides", {}).get(key)
    if skill_selection is not None:
        from services.skills import SkillError, render_active_skill_prompt

        try:
            active_skill_prompt = render_active_skill_prompt(skill_selection)
        except SkillError as exc:
            getattr(deps, "skill_overrides", {}).pop(key, None)
            log.warning("active skill cleared for /%s: %s", command.name, exc)

    try:
        from services.provider_router import call_with_provider, resolve_provider
        from services.session import shared_sessions

        sessions = shared_sessions()
        provider_overrides = getattr(deps, "provider_overrides", {})
        provider = resolve_provider(
            explicit=provider_overrides.get(key),
            model=model,
            restricted=False,
        )
        sema = deps.get_channel_sema(channel_id) if deps.get_channel_sema else None

        async def _invoke():
            return await call_with_provider(
                prepared.prompt,
                channel_id,
                sessions,
                provider=provider,
                restricted=False,  # admin-gated above → full tools (allowedTools ⊆ admin)
                raw_user_text=f"/{command.name} {args}".strip(),
                author=author,
                user_id=interaction.user.id,
                forced_model_override=model,
                forced_effort_override=effort,
                active_skill_prompt=active_skill_prompt,
            )

        if sema is not None:
            async with sema:
                response_text, _session, _resumed = await _invoke()
        else:
            response_text, _session, _resumed = await _invoke()
    except Exception as exc:
        log.exception("/%s tool turn failed for ch=%s", command.name, channel_id)
        await interaction.followup.send(f"`/{command.name}` の実行に失敗しました: {exc}")
        return

    text = (response_text or "").strip() or "（応答が空でした）"
    chunks = _chunk(text)
    truncated = len(chunks) > _MAX_CHUNKS
    chunks = chunks[:_MAX_CHUNKS]
    for i, chunk in enumerate(chunks):
        suffix = "\n-# 長いので途中まで" if (truncated and i == len(chunks) - 1) else ""
        await interaction.followup.send(f"{chunk}{suffix}")


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    commands = _load_commands()

    def _make(command):
        async def _handler(interaction: discord.Interaction, args: str = ""):
            await _run(interaction, deps, command, args)

        return _handler

    for name in _WIRED_COMMANDS:
        command = commands[name]
        handler = _make(command)
        handler = app_commands.describe(
            args="コマンド引数（free-code の /" + name + " と同じ。任意）"
        )(handler)
        tree.command(
            name=name,
            description=(command.description[:100] + " (admin only)")[:100],
        )(handler)

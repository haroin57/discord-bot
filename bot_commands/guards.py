"""Reusable authorization and Discord-context guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bot_commands.responses import ResponsePort


@dataclass(frozen=True)
class CommandContext:
    channel_id: int
    user_id: int


async def require_admin(
    response: ResponsePort,
    interaction: Any,
    is_admin: Callable[[Any], bool],
) -> bool:
    if is_admin(interaction.user):
        return True
    await response.send("admin only", ephemeral=True)
    return False


async def require_admin_channel(
    response: ResponsePort,
    interaction: Any,
    is_admin: Callable[[Any], bool],
) -> CommandContext | None:
    if not await require_admin(response, interaction, is_admin):
        return None
    channel_id = interaction.channel_id
    if channel_id is None:
        await response.send("チャンネル外では使えません", ephemeral=True)
        return None
    return CommandContext(channel_id=channel_id, user_id=interaction.user.id)

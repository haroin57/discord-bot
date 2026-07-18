"""Discord response boundary for command handlers."""

from __future__ import annotations

from typing import Any, Protocol


class ResponsePort(Protocol):
    """Minimal response contract used by Discord command handlers."""

    async def defer(self, *, ephemeral: bool = True) -> None: ...

    async def send(
        self,
        content: str,
        *,
        ephemeral: bool = True,
        **kwargs: Any,
    ) -> Any: ...


class InteractionResponse:
    """Route a command reply to the initial response or deferred follow-up."""

    def __init__(self, interaction: Any) -> None:
        self._interaction = interaction
        self._deferred = False
        self._responded = False

    def _initial_response_done(self) -> bool:
        is_done = getattr(self._interaction.response, "is_done", None)
        return bool(is_done()) if callable(is_done) else False

    async def defer(self, *, ephemeral: bool = True) -> None:
        await self._interaction.response.defer(ephemeral=ephemeral)
        self._deferred = True

    async def send(
        self,
        content: str,
        *,
        ephemeral: bool = True,
        **kwargs: Any,
    ) -> Any:
        if self._deferred or self._responded or self._initial_response_done():
            return await self._interaction.followup.send(
                content,
                ephemeral=ephemeral,
                **kwargs,
            )
        self._responded = True
        return await self._interaction.response.send_message(
            content,
            ephemeral=ephemeral,
            **kwargs,
        )

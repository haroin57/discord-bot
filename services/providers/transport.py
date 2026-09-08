"""Provider-specific adapters around the stable legacy entry points."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from services.providers.contracts import (
    ProviderBackend,
    ProviderCapability,
    ProviderDescriptor,
    ProviderRequest,
    ProviderResult,
)

LegacyProviderCall = Callable[..., Awaitable[tuple[str, Any | None, bool]]]
CallResolver = Callable[[], LegacyProviderCall]

CLAUDE_CAPABILITIES = frozenset(ProviderCapability)
CODEX_CAPABILITIES = frozenset(
    {
        ProviderCapability.STATUS,
        ProviderCapability.INCREMENTAL_TEXT,
        ProviderCapability.TEXT_STREAM,
        ProviderCapability.TOOL_INVOCATION,
        ProviderCapability.THINKING_STREAM,
        ProviderCapability.THINKING_FINALIZE,
        ProviderCapability.PROGRESS,
        ProviderCapability.SCRATCH_STREAM,
        ProviderCapability.SCRATCH_FINALIZE,
        ProviderCapability.VERIFY_STREAM,
        ProviderCapability.VERIFY_FINALIZE,
    }
)

CLAUDE_DESCRIPTOR = ProviderDescriptor(
    name="claude",
    label="Claude",
    capabilities=CLAUDE_CAPABILITIES,
)
CODEX_DESCRIPTOR = ProviderDescriptor(
    name="codex",
    label="Codex",
    capabilities=CODEX_CAPABILITIES,
)


@dataclass(frozen=True, slots=True)
class LegacyProviderTransport(ProviderBackend):
    descriptor: ProviderDescriptor
    resolve_call: CallResolver

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        callback_kwargs = {
            key: value
            for key, value in request.callbacks.for_capabilities(
                self.descriptor.capabilities,
            ).items()
            if value is not None
        }
        request_kwargs: dict[str, Any] = dict(callback_kwargs)
        optional_values = {
            "raw_user_text": request.raw_user_text,
            "author": request.author,
            "user_id": request.user_id,
            "forced_effort_override": request.forced_effort_override,
            "tier": request.tier,
            "active_skill_prompt": request.active_skill_prompt,
        }
        request_kwargs.update(
            {
                key: value
                for key, value in optional_values.items()
                if value is not None and value != ""
            }
        )
        if request.restricted:
            request_kwargs["restricted"] = True
        if request.is_thread:
            request_kwargs["is_thread"] = True
        if request.has_image_attachment:
            request_kwargs["has_image_attachment"] = True
        if request.route_judgment:
            request_kwargs["route_judgment"] = True
        if (
            request.forced_model_override is not None
            or request.forced_model_override_supplied
        ):
            request_kwargs["forced_model_override"] = request.forced_model_override
        text, session, resumed = await self.resolve_call()(
            request.prompt,
            request.channel_id,
            request.sessions,
            **request_kwargs,
        )
        return ProviderResult(
            text=text,
            session=session,
            resumed=resumed,
            provider=self.descriptor.name,
        )

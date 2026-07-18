"""Dependency-free contracts shared by provider policy and execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, TypeAlias

ProviderName: TypeAlias = Literal["claude", "codex"]
TextCallback: TypeAlias = Callable[[str], Awaitable[None]]
FinalizeCallback: TypeAlias = Callable[[], Awaitable[None]]
ToolInvocationCallback: TypeAlias = Callable[[str, dict[str, Any]], Awaitable[None]]
ScratchStreamCallback: TypeAlias = Callable[[str, str, str], Awaitable[None]]


class ProviderCapability(StrEnum):
    STATUS = "status"
    INCREMENTAL_TEXT = "incremental_text"
    TEXT_STREAM = "text_stream"
    TOOL_INVOCATION = "tool_invocation"
    SCRATCH_STREAM = "scratch_stream"
    SCRATCH_FINALIZE = "scratch_finalize"
    THINKING_STREAM = "thinking_stream"
    THINKING_FINALIZE = "thinking_finalize"
    VERIFY_STREAM = "verify_stream"
    VERIFY_FINALIZE = "verify_finalize"
    PROGRESS = "progress"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    name: ProviderName
    label: str
    capabilities: frozenset[ProviderCapability]


@dataclass(frozen=True, slots=True)
class TurnCallbacks:
    status_callback: TextCallback | None = None
    incremental_text_callback: TextCallback | None = None
    text_stream_callback: TextCallback | None = None
    tool_invocation_callback: ToolInvocationCallback | None = None
    scratch_stream_callback: ScratchStreamCallback | None = None
    scratch_finalize_callback: FinalizeCallback | None = None
    thinking_stream_callback: TextCallback | None = None
    thinking_finalize_callback: FinalizeCallback | None = None
    verify_stream_callback: TextCallback | None = None
    verify_finalize_callback: FinalizeCallback | None = None
    progress: dict[str, Any] | None = None

    def for_capabilities(
        self,
        capabilities: frozenset[ProviderCapability],
    ) -> dict[str, Any]:
        """Return the complete legacy callback shape, masking unsupported fields."""
        return {
            field_name: getattr(self, field_name)
            if capability in capabilities
            else None
            for field_name, capability in _CALLBACK_CAPABILITIES.items()
        }


_CALLBACK_CAPABILITIES: Mapping[str, ProviderCapability] = {
    "status_callback": ProviderCapability.STATUS,
    "incremental_text_callback": ProviderCapability.INCREMENTAL_TEXT,
    "text_stream_callback": ProviderCapability.TEXT_STREAM,
    "tool_invocation_callback": ProviderCapability.TOOL_INVOCATION,
    "scratch_stream_callback": ProviderCapability.SCRATCH_STREAM,
    "scratch_finalize_callback": ProviderCapability.SCRATCH_FINALIZE,
    "thinking_stream_callback": ProviderCapability.THINKING_STREAM,
    "thinking_finalize_callback": ProviderCapability.THINKING_FINALIZE,
    "verify_stream_callback": ProviderCapability.VERIFY_STREAM,
    "verify_finalize_callback": ProviderCapability.VERIFY_FINALIZE,
    "progress": ProviderCapability.PROGRESS,
}


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    prompt: Any
    channel_id: int
    sessions: Any
    callbacks: TurnCallbacks = field(default_factory=TurnCallbacks)
    restricted: bool = False
    raw_user_text: str = ""
    is_thread: bool = False
    has_image_attachment: bool = False
    author: str = ""
    user_id: int | None = None
    forced_model_override: str | None = None
    forced_model_override_supplied: bool = False
    forced_effort_override: str | None = None
    tier: str | None = None
    route_judgment: bool = False
    active_skill_prompt: str = ""


@dataclass(frozen=True, slots=True)
class ProviderResult:
    text: str
    session: Any | None
    resumed: bool
    provider: ProviderName

    def as_legacy_tuple(self) -> tuple[str, Any | None, bool]:
        return self.text, self.session, self.resumed


class ProviderBackend(Protocol):
    descriptor: ProviderDescriptor

    async def execute(self, request: ProviderRequest) -> ProviderResult: ...

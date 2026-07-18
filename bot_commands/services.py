"""Service protocols used by Discord command adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SideQuestionRequest:
    channel_id: int
    user_id: int
    question: str
    author: str
    provider: str | None = None
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class SideQuestionAnswer:
    answer: str
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    prior_turns: int = 0
    paused_reason: str | None = None


class SideQuestionService(Protocol):
    async def answer(self, request: SideQuestionRequest) -> SideQuestionAnswer: ...


class DefaultSideQuestionService:
    """Keep /btw Claude-backed while normalizing Codex-only overrides."""

    async def answer(self, request: SideQuestionRequest) -> SideQuestionAnswer:
        from services.claude_api.client_factory import make_api_client
        from services.claude_api.side_question import answer_side_question
        from services.provider_router import (
            PROVIDER_CODEX,
            is_codex_model,
            resolve_provider,
        )

        provider = resolve_provider(
            explicit=request.provider,
            model=request.model,
            restricted=False,
        )
        model = request.model
        effort = request.effort
        if provider == PROVIDER_CODEX or is_codex_model(model):
            model = None
            effort = None

        api_client = await make_api_client(restricted=False)
        result = await answer_side_question(
            request.channel_id,
            request.question,
            api_client,
            user_id=request.user_id,
            model=model,
            effort=effort,
            author=request.author,
        )
        return SideQuestionAnswer(
            answer=result.answer,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=result.duration_ms,
            prior_turns=result.prior_turns,
            paused_reason=result.paused_reason,
        )


@dataclass
class CommandServices:
    side_questions: SideQuestionService = field(
        default_factory=DefaultSideQuestionService
    )


def services_from_deps(deps: Any) -> CommandServices:
    services = getattr(deps, "command_services", None)
    if services is None:
        services = CommandServices()
        setattr(deps, "command_services", services)
    return services

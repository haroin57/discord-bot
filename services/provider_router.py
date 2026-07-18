"""Stable provider facade for the Discord bot's main agent turn."""

from __future__ import annotations

from typing import Any, cast

from config import DEFAULT_LLM_PROVIDER
from services.providers.composition import provider_workflow
from services.providers.contracts import (
    ProviderCapability,
    ProviderName,
    ProviderRequest,
    ProviderResult,
    TurnCallbacks,
)
from services.providers.policy import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDERS,
    is_codex_model,
    resolve_provider_name,
)

_PROVIDERS = PROVIDERS

_LEGACY_CALL_FIELDS = (
    "prompt",
    "channel_id",
    "sessions",
    "status_callback",
    "incremental_text_callback",
    "text_stream_callback",
    "tool_invocation_callback",
    "scratch_stream_callback",
    "scratch_finalize_callback",
    "thinking_stream_callback",
    "thinking_finalize_callback",
    "verify_stream_callback",
    "verify_finalize_callback",
    "progress",
    "restricted",
    "raw_user_text",
    "is_thread",
    "has_image_attachment",
    "author",
    "user_id",
    "forced_model_override",
    "forced_effort_override",
    "tier",
    "route_judgment",
    "active_skill_prompt",
)
_LEGACY_CALL_DEFAULTS: dict[str, Any] = {
    "status_callback": None,
    "incremental_text_callback": None,
    "text_stream_callback": None,
    "tool_invocation_callback": None,
    "scratch_stream_callback": None,
    "scratch_finalize_callback": None,
    "thinking_stream_callback": None,
    "thinking_finalize_callback": None,
    "verify_stream_callback": None,
    "verify_finalize_callback": None,
    "progress": None,
    "restricted": False,
    "raw_user_text": "",
    "is_thread": False,
    "has_image_attachment": False,
    "author": "",
    "user_id": None,
    "forced_model_override": None,
    "forced_effort_override": None,
    "tier": None,
    "route_judgment": False,
    "active_skill_prompt": "",
}


def resolve_provider(
    *,
    explicit: str | None = None,
    model: str | None = None,
    restricted: bool = False,
) -> str:
    return resolve_provider_name(
        explicit=explicit,
        model=model,
        restricted=restricted,
        default=DEFAULT_LLM_PROVIDER,
    )


def provider_label(provider: str) -> str:
    return "Codex" if provider == PROVIDER_CODEX else "Claude"


def provider_capabilities(provider: str) -> frozenset[ProviderCapability]:
    return provider_workflow.dispatcher.capabilities(_provider_name(provider))


def provider_resume_hint(
    provider: str,
    channel_id: int,
    user_id: int | None,
) -> bool | None:
    """Return a provider-owned resume hint before prompt construction.

    Claude keeps the existing session/freshness decision in the bot context
    stage. Codex owns a separate durable thread, but a continuity invalidation
    must win over the presence of that stale thread so the bot builds a full
    cold-start prompt before the workflow retires it.
    """
    provider_name = _provider_name(provider)
    if provider_name != PROVIDER_CODEX:
        return None

    from services.session import is_provider_continuity_invalid

    if is_provider_continuity_invalid(
        PROVIDER_CODEX,
        channel_id,
        user_id,
    ):
        return False

    from services.codex_app_server.manager import get_manager

    return get_manager().has_thread(channel_id, user_id)


async def call_provider(
    request: ProviderRequest,
    *,
    provider: str,
) -> ProviderResult:
    return await provider_workflow.run(request, _provider_name(provider))


async def call_with_provider(
    *args: Any,
    provider: str,
    **kwargs: Any,
) -> tuple[str, Any | None, bool]:
    """Compatibility facade normalized into the typed provider dispatcher.

    The former facade forwarded ``*args`` directly to the provider entry point,
    so callbacks after ``sessions`` could be positional on the Claude route.
    Keep that public shape while constructing one typed ``ProviderRequest`` for
    both providers.
    """
    request = _normalize_legacy_call(args, kwargs)
    return (await call_provider(request, provider=provider)).as_legacy_tuple()


def _normalize_legacy_call(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> ProviderRequest:
    if len(args) > len(_LEGACY_CALL_FIELDS):
        raise TypeError(
            f"call_with_provider() received too many positional arguments: {len(args)}"
        )

    values = dict(zip(_LEGACY_CALL_FIELDS, args, strict=False))
    duplicate = next((name for name in values if name in kwargs), None)
    if duplicate is not None:
        raise TypeError(
            f"call_with_provider() got multiple values for argument {duplicate!r}"
        )

    unexpected = next(
        (name for name in kwargs if name not in _LEGACY_CALL_FIELDS),
        None,
    )
    if unexpected is not None:
        raise TypeError(
            f"call_with_provider() got an unexpected keyword argument {unexpected!r}"
        )
    values.update(kwargs)

    missing = [name for name in _LEGACY_CALL_FIELDS[:3] if name not in values]
    if missing:
        raise TypeError(
            "call_with_provider() missing required argument(s): "
            + ", ".join(repr(name) for name in missing)
        )
    for name, default in _LEGACY_CALL_DEFAULTS.items():
        values.setdefault(name, default)

    request = ProviderRequest(
        prompt=values["prompt"],
        channel_id=values["channel_id"],
        sessions=values["sessions"],
        callbacks=TurnCallbacks(
            status_callback=values["status_callback"],
            incremental_text_callback=values["incremental_text_callback"],
            text_stream_callback=values["text_stream_callback"],
            tool_invocation_callback=values["tool_invocation_callback"],
            scratch_stream_callback=values["scratch_stream_callback"],
            scratch_finalize_callback=values["scratch_finalize_callback"],
            thinking_stream_callback=values["thinking_stream_callback"],
            thinking_finalize_callback=values["thinking_finalize_callback"],
            verify_stream_callback=values["verify_stream_callback"],
            verify_finalize_callback=values["verify_finalize_callback"],
            progress=values["progress"],
        ),
        restricted=values["restricted"],
        raw_user_text=values["raw_user_text"],
        is_thread=values["is_thread"],
        has_image_attachment=values["has_image_attachment"],
        author=values["author"],
        user_id=values["user_id"],
        forced_model_override=values["forced_model_override"],
        forced_model_override_supplied=(values["forced_model_override"] is not None),
        forced_effort_override=values["forced_effort_override"],
        tier=values["tier"],
        route_judgment=values["route_judgment"],
        active_skill_prompt=values["active_skill_prompt"],
    )
    return request


def _provider_name(provider: str) -> ProviderName:
    if provider not in _PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    return cast(ProviderName, provider)

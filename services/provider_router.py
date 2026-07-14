"""Provider-neutral dispatch for the Discord bot's main agent turn."""

from __future__ import annotations

import logging
from typing import Any

from config import DEFAULT_LLM_PROVIDER

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
_PROVIDERS = {PROVIDER_CLAUDE, PROVIDER_CODEX}
log = logging.getLogger(__name__)


def is_codex_model(model: str | None) -> bool:
    value = (model or "").strip().lower()
    return value.startswith(("gpt-", "o1", "o3", "o4"))


def resolve_provider(
    *,
    explicit: str | None = None,
    model: str | None = None,
    restricted: bool = False,
) -> str:
    # Codex native tools are not exposed to untrusted users. This hard gate is
    # independent of slash-command authorization and environment defaults.
    if restricted:
        return PROVIDER_CLAUDE
    value = (explicit or "").strip().lower()
    if value in _PROVIDERS:
        return value
    if is_codex_model(model):
        return PROVIDER_CODEX
    default = (DEFAULT_LLM_PROVIDER or PROVIDER_CLAUDE).strip().lower()
    return default if default in _PROVIDERS else PROVIDER_CLAUDE


def provider_label(provider: str) -> str:
    return "Codex" if provider == PROVIDER_CODEX else "Claude"


async def call_with_provider(
    *args: Any,
    provider: str,
    **kwargs: Any,
):
    if provider not in _PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    # Model overrides are shared with the legacy Claude route. A later
    # /provider switch must not hand a GPT slug to Anthropic or a Claude slug
    # to Codex. Unknown custom IDs are left untouched for provider experiments.
    model = kwargs.get("forced_model_override")
    if model:
        if provider == PROVIDER_CODEX and not is_codex_model(str(model)):
            kwargs["forced_model_override"] = None
        elif provider == PROVIDER_CLAUDE and is_codex_model(str(model)):
            kwargs["forced_model_override"] = None
    if provider == PROVIDER_CODEX:
        from services.codex_app_server.entry import call_with_tools
    else:
        from services.claude_api import call_with_tools
    result = await call_with_tools(*args, **kwargs)

    # Only one provider thread may remain authoritative for a channel/user.
    # The completed provider already appended its turn to the shared JSONL.
    # Retiring the dormant provider makes the next switch rebuild from that
    # transcript instead of resuming a thread that missed intervening turns.
    if len(args) >= 2:
        channel_id = args[1]
        user_id = kwargs.get("user_id")
        try:
            if provider == PROVIDER_CODEX:
                from services.session import clear_session

                clear_session(int(channel_id), user_id)
            else:
                from services.codex_app_server.manager import clear_codex_thread

                await clear_codex_thread(int(channel_id), user_id)
        except Exception as exc:
            # Provider output is already complete. Session-coherence cleanup is
            # repairable on the next turn and must not discard a valid reply.
            log.warning(
                "Dormant provider session cleanup failed for channel=%s: %s",
                channel_id,
                exc,
            )
    return result

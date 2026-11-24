"""Pure provider selection and request-normalization policy."""

from __future__ import annotations

from dataclasses import replace
import os

from services.providers.contracts import ProviderName, ProviderRequest

PROVIDER_CLAUDE: ProviderName = "claude"
PROVIDER_CODEX: ProviderName = "codex"
PROVIDERS: frozenset[ProviderName] = frozenset({PROVIDER_CLAUDE, PROVIDER_CODEX})


def is_codex_model(model: str | None) -> bool:
    value = (model or "").strip().lower()
    return value.startswith(("gpt-", "o1", "o3", "o4"))


def resolve_provider_name(
    *,
    explicit: str | None,
    model: str | None,
    restricted: bool,
    default: str | None,
) -> ProviderName:
    if restricted:
        if ((default or '').strip().lower() == PROVIDER_CODEX
                and os.environ.get('CODEX_RESTRICTED_ENABLED', '').lower() in {'1', 'true'}):
            return PROVIDER_CODEX
        return PROVIDER_CLAUDE
    value = (explicit or "").strip().lower()
    if value in PROVIDERS:
        return value
    if is_codex_model(model):
        return PROVIDER_CODEX
    normalized_default = (default or PROVIDER_CLAUDE).strip().lower()
    if normalized_default in PROVIDERS:
        return normalized_default
    return PROVIDER_CLAUDE


def normalize_request_for_provider(
    request: ProviderRequest,
    provider: ProviderName,
) -> ProviderRequest:
    model = request.forced_model_override
    if not model:
        return request
    incompatible = (provider == PROVIDER_CODEX and not is_codex_model(model)) or (
        provider == PROVIDER_CLAUDE and is_codex_model(model)
    )
    return replace(request, forced_model_override=None) if incompatible else request

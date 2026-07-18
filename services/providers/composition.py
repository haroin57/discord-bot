"""Composition root for the provider subsystem."""

from __future__ import annotations

from services.providers.execution import ProviderDispatcher
from services.providers.transport import (
    CLAUDE_DESCRIPTOR,
    CODEX_DESCRIPTOR,
    LegacyProviderCall,
    LegacyProviderTransport,
)
from services.providers.workflow import ProviderWorkflow


def _resolve_claude_call() -> LegacyProviderCall:
    from services import claude_api

    return claude_api.call_with_tools


def _resolve_codex_call() -> LegacyProviderCall:
    from services.codex_app_server import entry

    return entry.call_with_tools


def build_provider_workflow() -> ProviderWorkflow:
    claude = LegacyProviderTransport(CLAUDE_DESCRIPTOR, _resolve_claude_call)
    codex = LegacyProviderTransport(CODEX_DESCRIPTOR, _resolve_codex_call)
    return ProviderWorkflow(ProviderDispatcher({"claude": claude, "codex": codex}))


provider_workflow = build_provider_workflow()

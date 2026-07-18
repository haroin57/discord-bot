"""Typed provider dispatch independent of provider lifecycle side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from services.providers.contracts import (
    ProviderBackend,
    ProviderCapability,
    ProviderName,
    ProviderRequest,
    ProviderResult,
)
from services.providers.policy import normalize_request_for_provider


@dataclass(frozen=True, slots=True)
class ProviderDispatcher:
    backends: Mapping[ProviderName, ProviderBackend]

    def __post_init__(self) -> None:
        for name, backend in self.backends.items():
            if backend.descriptor.name != name:
                raise ValueError(
                    f"provider registry key {name!r} does not match "
                    f"descriptor {backend.descriptor.name!r}"
                )

    def capabilities(self, provider: ProviderName) -> frozenset[ProviderCapability]:
        return self._backend(provider).descriptor.capabilities

    async def dispatch(
        self,
        request: ProviderRequest,
        provider: ProviderName,
    ) -> ProviderResult:
        normalized = normalize_request_for_provider(request, provider)
        return await self._backend(provider).execute(normalized)

    def _backend(self, provider: ProviderName) -> ProviderBackend:
        try:
            return self.backends[provider]
        except KeyError as exc:
            raise ValueError(f"unsupported provider: {provider!r}") from exc

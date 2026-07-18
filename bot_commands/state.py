"""Typed state boundary for channel/user command overrides."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Protocol

OverrideKey = tuple[int, int]


class CommandState(Protocol):
    def model(self, key: OverrideKey) -> str | None: ...
    def set_model(self, key: OverrideKey, value: str) -> None: ...
    def clear_model(self, key: OverrideKey) -> None: ...
    def effort(self, key: OverrideKey) -> str | None: ...
    def set_effort(self, key: OverrideKey, value: str) -> None: ...
    def clear_effort(self, key: OverrideKey) -> None: ...
    def provider(self, key: OverrideKey) -> str | None: ...
    def set_provider(self, key: OverrideKey, value: str) -> None: ...
    def clear_provider(self, key: OverrideKey) -> None: ...
    def tier(self, key: OverrideKey) -> str | None: ...
    def set_tier(self, key: OverrideKey, value: str) -> None: ...
    def clear_tier(self, key: OverrideKey) -> None: ...
    def skill(self, key: OverrideKey) -> Any: ...
    def set_skill(self, key: OverrideKey, value: Any) -> None: ...
    def clear_skill(self, key: OverrideKey) -> Any: ...
    def clear_channel_skills(self, channel_id: int) -> int: ...
    def skill_mapping(self) -> MutableMapping[OverrideKey, Any]: ...


class MappingCommandState:
    """Adapter over bot-owned live dictionaries."""

    def __init__(
        self,
        *,
        models: MutableMapping[OverrideKey, str],
        efforts: MutableMapping[OverrideKey, str],
        providers: MutableMapping[OverrideKey, str],
        tiers: MutableMapping[OverrideKey, str],
        skills: MutableMapping[OverrideKey, Any],
    ) -> None:
        self._models = models
        self._efforts = efforts
        self._providers = providers
        self._tiers = tiers
        self._skills = skills

    def model(self, key: OverrideKey) -> str | None:
        return self._models.get(key)

    def set_model(self, key: OverrideKey, value: str) -> None:
        self._models[key] = value

    def clear_model(self, key: OverrideKey) -> None:
        self._models.pop(key, None)

    def effort(self, key: OverrideKey) -> str | None:
        return self._efforts.get(key)

    def set_effort(self, key: OverrideKey, value: str) -> None:
        self._efforts[key] = value

    def clear_effort(self, key: OverrideKey) -> None:
        self._efforts.pop(key, None)

    def provider(self, key: OverrideKey) -> str | None:
        return self._providers.get(key)

    def set_provider(self, key: OverrideKey, value: str) -> None:
        self._providers[key] = value

    def clear_provider(self, key: OverrideKey) -> None:
        self._providers.pop(key, None)

    def tier(self, key: OverrideKey) -> str | None:
        return self._tiers.get(key)

    def set_tier(self, key: OverrideKey, value: str) -> None:
        self._tiers[key] = value

    def clear_tier(self, key: OverrideKey) -> None:
        self._tiers.pop(key, None)

    def skill(self, key: OverrideKey) -> Any:
        return self._skills.get(key)

    def set_skill(self, key: OverrideKey, value: Any) -> None:
        self._skills[key] = value

    def clear_skill(self, key: OverrideKey) -> Any:
        return self._skills.pop(key, None)

    def clear_channel_skills(self, channel_id: int) -> int:
        keys = [key for key in self._skills if key[0] == channel_id]
        for key in keys:
            self._skills.pop(key, None)
        return len(keys)

    def skill_mapping(self) -> MutableMapping[OverrideKey, Any]:
        return self._skills


def _mapping(deps: Any, name: str) -> MutableMapping:
    mapping = getattr(deps, name, None)
    if mapping is None:
        mapping = {}
        setattr(deps, name, mapping)
    return mapping


def state_from_deps(deps: Any) -> CommandState:
    state = getattr(deps, "command_state", None)
    if state is not None:
        return state
    state = MappingCommandState(
        models=_mapping(deps, "model_overrides"),
        efforts=_mapping(deps, "effort_overrides"),
        providers=_mapping(deps, "provider_overrides"),
        tiers=_mapping(deps, "tier_overrides"),
        skills=_mapping(deps, "skill_overrides"),
    )
    setattr(deps, "command_state", state)
    return state

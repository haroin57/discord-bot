"""Private authorization boundary for managed derived-index writes."""

from __future__ import annotations

import os


class _ProjectionCapability:
    __slots__ = ()


_INTERNAL_CAPABILITY = _ProjectionCapability()


def _managed_mode() -> bool:
    return os.environ.get("MEM0_INTEGRITY_MODE", "legacy").lower() in {
        "shadow",
        "canonical",
    }


def _internal_projection_capability() -> _ProjectionCapability:
    """Return the capability used only by the canonical outbox consumer."""
    return _INTERNAL_CAPABILITY


def reject_direct_mutation(projection: str) -> None:
    if _managed_mode():
        raise RuntimeError(
            f"direct {projection} mutation is disabled in managed mode",
        )


def require_internal_projection(capability: object, projection: str) -> None:
    if _managed_mode() and capability is not _INTERNAL_CAPABILITY:
        raise RuntimeError(
            f"{projection} projection mutation requires internal capability",
        )

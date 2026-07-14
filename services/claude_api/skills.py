"""Compatibility facade for the Claude built-in Skill tool.

Provider-neutral discovery lives in :mod:`services.skills`; this module keeps
the original import surface used by ``services.claude_api.tools``.
"""

from __future__ import annotations

from services.skills import Skill, build_invocation as _build_invocation
from services.skills import format_listing as _format_listing
from services.skills import load_skills as _load_skills


def load_skills(force: bool = False) -> dict[str, Skill]:
    return _load_skills(force=force)


def format_listing() -> str:
    return _format_listing(invocation="model")


def build_invocation(skill_name: str, args: str = "") -> str:
    return _build_invocation(skill_name, args, invocation="model")

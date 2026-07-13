"""Registry assembly — Python analogue of ``commands.ts``'s ``COMMANDS`` list.

Gathers every ported command module's ``COMMANDS`` into one
:class:`~free_code_commands.registry.CommandRegistry`. Import order mirrors
``commands.ts`` loosely (prompt → local → local-jsx → feature-gated → internal),
which is all that matters since the registry resolves by name/alias, not position.

The interactive/non-interactive twins (``context``, ``extra-usage``) and the
ant/feature stubs are all registered; ``CommandRegistry.resolve`` and the
``enabled``/``hidden`` predicates disambiguate them per runtime, exactly as
``getCommands()`` filters the TS list at call time.
"""

from __future__ import annotations

from .commands import (
    feature_gated,
    internal_commands,
    jsx_commands,
    local_commands,
    prompt_commands,
)
from .registry import CommandRegistry
from .types import Command

# Order mirrors commands.ts groupings. Duplicates-by-name (twins) are intentional.
_MODULES = (
    prompt_commands,
    local_commands,
    jsx_commands,
    feature_gated,
    internal_commands,
)


def all_commands() -> list[Command]:
    """Every ported command instance, in module order."""

    out: list[Command] = []
    for mod in _MODULES:
        out.extend(mod.COMMANDS)
    return out


def build_registry() -> CommandRegistry:
    """Build the full :class:`CommandRegistry` of ported commands."""

    return CommandRegistry(all_commands())


# Convenience: names grouped by module, handy for tests and introspection.
def commands_by_module() -> dict[str, list[str]]:
    return {
        "prompt": [c.name for c in prompt_commands.COMMANDS],
        "local": [c.name for c in local_commands.COMMANDS],
        "local-jsx": [c.name for c in jsx_commands.COMMANDS],
        "feature-gated": [c.name for c in feature_gated.COMMANDS],
        "internal": [c.name for c in internal_commands.COMMANDS],
    }

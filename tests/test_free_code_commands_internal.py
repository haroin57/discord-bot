"""PoC: internal-only (ant) commands are faithful stubs.

In this distribution every INTERNAL_ONLY command's upstream ``index.js`` is the
shim ``{ isEnabled: () => false, isHidden: true, name: 'stub' }`` — the real
tooling is stripped from the public build. The port must reproduce that exactly:
disabled, hidden, and returning the stub notice — never a fabricated implementation.
"""

from __future__ import annotations

from free_code_commands import build_registry
from free_code_commands.commands import internal_commands as ic
from free_code_commands.runtime import RecordingRuntime


def test_all_internal_commands_disabled_and_hidden():
    for cmd in ic.COMMANDS:
        assert cmd.hidden is True, cmd.name
        assert not hasattr(cmd, "is_enabled_fn")
        assert not hasattr(cmd, "is_hidden_fn")


async def test_internal_stub_cannot_execute_through_public_boundary():
    registry = build_registry()
    for cmd in ic.COMMANDS:
        invocation = await registry.invoke(
            f"/{cmd.name}",
            RecordingRuntime(user_type="ant"),
        )
        assert invocation is None


def test_internal_commands_never_surface_in_active_listing():
    from free_code_commands import build_registry

    reg = build_registry()
    ant = RecordingRuntime(user_type="ant")
    active_names = {c.name for c in reg.active(ant)}
    # Internal names don't overlap any enabled command, so disabled → absent.
    for cmd in ic.COMMANDS:
        assert cmd.name not in active_names, cmd.name

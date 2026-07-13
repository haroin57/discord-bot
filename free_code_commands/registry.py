"""CommandRegistry — dispatch + filtering. Port of the relevant parts of
``src/commands.ts`` (``findCommand`` / ``getCommand`` / ``hasCommand`` /
``meetsAvailabilityRequirement`` / ``getCommands``).
"""

from __future__ import annotations

from typing import Iterable, Optional

from .runtime import CommandRuntime
from .types import Command


class CommandRegistry:
    """Holds all commands; resolves them by name/alias and filters by state.

    Duplicate names are permitted (the TS registry has interactive/non-interactive
    twins, e.g. ``context`` and ``extra-usage``); resolution disambiguates using
    the runtime's interactivity, falling back to the first registered match.
    """

    def __init__(self, commands: Optional[Iterable[Command]] = None) -> None:
        self._commands: list[Command] = list(commands or [])

    # ---- registration ----------------------------------------------------
    def register(self, command: Command) -> "CommandRegistry":
        self._commands.append(command)
        return self

    def extend(self, commands: Iterable[Command]) -> "CommandRegistry":
        self._commands.extend(commands)
        return self

    def all(self) -> list[Command]:
        """Every registered command, in registration order (like ``COMMANDS()``)."""

        return list(self._commands)

    def __len__(self) -> int:
        return len(self._commands)

    def __iter__(self):
        return iter(self._commands)

    # ---- lookup (findCommand / getCommand / hasCommand) ------------------
    def find(self, name: str) -> Optional[Command]:
        """Port of ``findCommand``: match on name, user-facing name, or alias."""

        for c in self._commands:
            if c.name == name or c.user_facing_name() == name or name in c.aliases:
                return c
        return None

    def has(self, name: str) -> bool:
        return self.find(name) is not None

    def get(self, name: str) -> Command:
        """Port of ``getCommand`` — raises with the available-command list on miss."""

        cmd = self.find(name)
        if cmd is None:
            available = sorted(
                (
                    f"{c.user_facing_name()} (aliases: {', '.join(c.aliases)})"
                    if c.aliases
                    else c.user_facing_name()
                )
                for c in self._commands
            )
            raise KeyError(
                f"Command {name} not found. Available commands: {', '.join(available)}"
            )
        return cmd

    # ---- availability / enablement --------------------------------------
    def meets_availability(self, cmd: Command, rt: CommandRuntime) -> bool:
        """Port of ``meetsAvailabilityRequirement``.

        No ``availability`` => available everywhere. ``claude-ai`` requires a
        subscriber; ``console`` requires a first-party API-key user who is neither
        a subscriber nor on 3P services (approximated here by API-key auth).
        """

        if not cmd.availability:
            return True
        for a in cmd.availability:
            if a == "claude-ai" and rt.is_claude_ai_subscriber():
                return True
            if a == "console" and (
                not rt.is_claude_ai_subscriber() and rt.has_anthropic_api_key_auth()
            ):
                return True
        return False

    def active(self, rt: CommandRuntime, *, include_hidden: bool = True) -> list[Command]:
        """Commands enabled and available for this runtime (port of ``getCommands``)."""

        out = []
        for c in self._commands:
            if not c.enabled(rt):
                continue
            if not self.meets_availability(c, rt):
                continue
            if not include_hidden and c.hidden(rt):
                continue
            out.append(c)
        return out

    def resolve(self, name: str, rt: CommandRuntime) -> Optional[Command]:
        """Resolve a name to the command whose enablement matches this runtime.

        Handles the interactive/non-interactive twins: among same-named matches,
        prefer one that is enabled for ``rt``; else fall back to :meth:`find`.
        """

        candidates = [
            c
            for c in self._commands
            if c.name == name or c.user_facing_name() == name or name in c.aliases
        ]
        if not candidates:
            return None
        for c in candidates:
            if c.enabled(rt):
                return c
        return candidates[0]

    def parse(self, text: str) -> tuple[Optional[Command], str]:
        """Split a ``/name args...`` line into its command and argument string.

        The leading ``/`` is optional, so ``"cost"`` and ``"/cost"`` both resolve.
        Returns ``(None, "")`` for empty input or an unknown name; args are
        everything after the first whitespace run, verbatim.
        """

        if not text:
            return None, ""
        stripped = text.lstrip()
        if stripped.startswith("/"):
            stripped = stripped[1:]
        if not stripped:
            return None, ""
        parts = stripped.split(None, 1)
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        return self.find(name), args

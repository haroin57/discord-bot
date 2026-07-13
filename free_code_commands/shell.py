r"""Prompt shell interpolation + argument substitution.

Port of ``src/utils/promptShellExecution.ts`` (the ``!`cmd``` / ```` ```! ``` ````
expansion) plus the ``$ARGUMENTS`` / ``$1`` substitution used by custom/markdown
commands.

Upstream patterns:

* block:  ``/```!\s*\n?([\s\S]*?)\n?```/g``
* inline: ``/(?<=^|\s)!`([^`]+)`/gm`` — a ``!`` preceded by start-of-line or
  whitespace, then a backtick-delimited command.

Python's ``re`` rejects the variable-width lookbehind ``(?<=^|\s)``, so the inline
scan captures the preceding boundary char and re-emits it. The gating optimisation
from the TS (only scan for inline when ``!`` `` `` is present) is preserved.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import CommandRuntime

from .runtime import ShellResult

_BLOCK_PATTERN = re.compile(r"```!\s*\n?([\s\S]*?)\n?```")
# Capture the boundary char (start or whitespace) in group 1, the command in 2.
_INLINE_PATTERN = re.compile(r"(^|\s)!`([^`]+)`", re.MULTILINE)


class MalformedCommandError(Exception):
    """Raised when a shell interpolation is denied by the runtime permission gate."""


def _format_shell_output(res: ShellResult) -> str:
    """Inline representation of a shell result.

    The CLI persists a full tool-result block; for prompt interpolation what
    matters is the text the model sees. We inline stdout, appending stderr when
    present, matching the effective content a Bash tool-result contributes.
    """

    parts: list[str] = []
    if res.stdout:
        parts.append(res.stdout.rstrip("\n"))
    if res.stderr:
        parts.append(res.stderr.rstrip("\n"))
    if res.interrupted:
        parts.append("[command interrupted]")
    return "\n".join(parts)


async def execute_shell_commands_in_prompt(
    text: str,
    runtime: "CommandRuntime",
    slash_command_name: str,
) -> str:
    """Replace embedded shell commands in ``text`` with their output.

    Mirrors ``executeShellCommandsInPrompt``: both block and inline forms are
    expanded; each command is permission-checked via ``runtime.can_run_shell``
    before running. A denied command raises :class:`MalformedCommandError`, as the
    TS raises ``MalformedCommandError``.
    """

    replacements: list[tuple[str, str]] = []

    async def _resolve(command: str) -> str:
        command = command.strip()
        if not command:
            return ""
        if not runtime.can_run_shell(command):
            raise MalformedCommandError(
                f"Shell command permission check failed for {slash_command_name}: {command}"
            )
        res = await runtime.run_shell(command)
        return _format_shell_output(res)

    # Block form first (always scanned).
    for m in _BLOCK_PATTERN.finditer(text):
        output = await _resolve(m.group(1) or "")
        replacements.append((m.group(0), output))

    # Inline form — gated on a cheap substring check, like the TS.
    if "!`" in text:
        for m in _INLINE_PATTERN.finditer(text):
            output = await _resolve(m.group(2) or "")
            # Preserve the boundary char (group 1) the lookbehind would have kept.
            replacements.append((m.group(0), f"{m.group(1)}{output}"))

    result = text
    for needle, value in replacements:
        result = result.replace(needle, value, 1)
    return result


def substitute_arguments(
    text: str,
    args: str,
    arg_names: Optional[list[str]] = None,
) -> str:
    """Substitute ``$ARGUMENTS`` / ``$1`` / ``$NAME`` placeholders in a template.

    Semantics match free-code's custom-command argument handling:

    * ``$ARGUMENTS`` — the full raw argument string.
    * ``$1``, ``$2``, ... — positional args split on whitespace (1-indexed).
    * ``$NAME`` — when ``arg_names`` is given, the positional arg at that name's
      index. Unknown names are left untouched.

    If a template contains no ``$ARGUMENTS`` and no ``$N`` placeholders, the raw
    args are appended (the upstream fallback so bare args are never dropped).
    """

    positional = args.split()
    had_placeholder = "$ARGUMENTS" in text or re.search(r"\$\d+", text) is not None

    out = text.replace("$ARGUMENTS", args)

    if arg_names:
        had_placeholder = had_placeholder or any(f"${n}" in out for n in arg_names)
        for i, name in enumerate(arg_names):
            value = positional[i] if i < len(positional) else ""
            out = out.replace(f"${name}", value)

    def _pos(m: "re.Match[str]") -> str:
        idx = int(m.group(1)) - 1
        return positional[idx] if 0 <= idx < len(positional) else ""

    out = re.sub(r"\$(\d+)", _pos, out)

    if not had_placeholder and args:
        out = f"{out}\n\n{args}" if out else args
    return out

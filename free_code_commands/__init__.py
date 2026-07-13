"""free_code_commands — a faithful Python port of free-code's slash-command system.

This package reimplements, in Python, the slash-command architecture found in
free-code's ``src/commands`` (a Claude-Code-family CLI written in TypeScript/Bun):

* the three command flavours — ``local`` / ``prompt`` / ``local-jsx`` — with the
  same metadata surface (name, aliases, description, argumentHint, availability,
  isEnabled/isHidden, ...);
* the prompt-expansion machinery: inline ``!`cmd``` / block ```` ```! cmd ``` ````
  shell interpolation and ``$ARGUMENTS`` / ``$1`` substitution;
* a :class:`~free_code_commands.registry.CommandRegistry` that dispatches by name
  or alias and filters by enablement/availability.

Runtime-bound behaviour (auth state, git, the compaction engine, the Ink TUI) is
abstracted behind :class:`~free_code_commands.runtime.CommandRuntime` so every
command is testable headlessly — mirroring the TypeScript ``ToolUseContext`` that
each ``call`` / ``getPromptForCommand`` receives.

The upstream sources live at ``/home/ubuntu/free-code/src/commands``; each ported
module names the file it was translated from.
"""

from .types import (
    Command,
    CommandType,
    ContentBlock,
    LocalCommand,
    LocalCommandResult,
    LocalJsxCommand,
    JsxOutcome,
    PromptCommand,
    text_block,
)
from .runtime import CommandRuntime, RecordingRuntime, ShellResult
from .shell import execute_shell_commands_in_prompt, substitute_arguments
from .registry import CommandRegistry

__all__ = [
    "Command",
    "CommandType",
    "ContentBlock",
    "LocalCommand",
    "LocalCommandResult",
    "LocalJsxCommand",
    "JsxOutcome",
    "PromptCommand",
    "text_block",
    "CommandRuntime",
    "RecordingRuntime",
    "ShellResult",
    "execute_shell_commands_in_prompt",
    "substitute_arguments",
    "CommandRegistry",
    "build_registry",
]


def build_registry() -> "CommandRegistry":
    """Build the full registry of ported commands (mirrors ``commands.ts``).

    Imported lazily so that merely importing the framework core does not pull in
    every command module.
    """
    from ._all import build_registry as _build

    return _build()

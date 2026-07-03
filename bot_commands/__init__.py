"""Slash-command modules split out of bot.py (P2-d).

Design (arbitrated in docs/2026-07-03-refactor-arbitration-gpt55pro.md):
no Cogs — plain modules with an explicit register(tree, deps) each, wired
by register_all(). Command names/permissions/behavior are unchanged from
their bot.py originals; tree.sync timing stays owned by bot.py's on_ready.
"""

from __future__ import annotations

from bot_commands import memory, misc, model_cmds, session_cmds
from bot_commands._deps import CommandDeps

__all__ = ["CommandDeps", "register_all"]


def register_all(tree, deps: CommandDeps) -> None:
    misc.register(tree, deps)
    memory.register(tree, deps)
    session_cmds.register(tree, deps)
    model_cmds.register(tree, deps)

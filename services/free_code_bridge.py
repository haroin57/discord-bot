"""Bridge: run a ``free_code_commands`` prompt command through the bot's turn.

This reproduces free-code's inline prompt-slash-command algorithm
(``getMessagesForPromptSlashCommand`` in
``src/utils/processUserInput/processSlashCommand.tsx``) so that ``/commit`` &c.
behave in Discord exactly as they do in the CLI. The reference steps are:

1. ``result = await command.getPromptForCommand(args, context)`` — expand the
   template, running embedded ``!`cmd``` **in the cwd**.
2. ``metadata = formatSlashCommandLoadingMetadata(name, args)`` — the
   ``<command-message>`` / ``<command-name>`` / ``<command-args>`` tags.
3. ``additionalAllowedTools = parseToolListFromCLI(command.allowedTools ?? [])``.
4. build ``[user(metadata), user(result, isMeta), command_permissions(allowedTools)]``.
5. ``shouldQuery: true`` → the main loop runs the model **with tools**.

This module is the algorithm's home. It has **no discord import** and no bot
dependency, so its output (prompt text + allowed tools + tags) can be asserted
against the free-code shape in unit tests. The Discord layer
(``bot_commands/free_code_slash.py``) feeds ``PreparedSlashTurn.prompt`` to
``call_with_tools`` — the bot's equivalent of step 5.

The bot's admin already has unrestricted tools (CLAUDE.md), so step 4's
permission widening (``command_permissions``) is satisfied by the admin context;
``allowed_tools`` is still surfaced for auditing and parity assertions.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional

from free_code_commands.runtime import Attribution, CommandRuntime, ShellResult
from free_code_commands.types import Command, ContentBlock

# Commit/PR trailers the bot stamps (CLAUDE.md git convention). free-code sources
# these from getAttributionTexts(); here they are the bot's standard trailers.
_COMMIT_ATTRIBUTION = "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
_PR_ATTRIBUTION = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


# ---------------------------------------------------------------------------
# Step 3 — parseToolListFromCLI (verbatim port of permissionSetup.ts)
# ---------------------------------------------------------------------------


def parse_tool_list_from_cli(tools: list[str]) -> list[str]:
    """Port of ``parseToolListFromCLI``.

    Splits each entry on top-level commas while keeping commas inside parens
    intact (so ``Bash(git add:*)`` stays one token and ``A,B`` becomes two).
    """

    if not tools:
        return []
    result: list[str] = []
    for tool_string in tools:
        if not tool_string:
            continue
        current = ""
        in_parens = False
        for ch in tool_string:
            if ch == "(":
                in_parens = True
                current += ch
            elif ch == ")":
                in_parens = False
                current += ch
            elif ch == ",":
                if in_parens:
                    current += ch
                elif current.strip():
                    result.append(current.strip())
                    current = ""
            else:
                current += ch
        if current.strip():
            result.append(current.strip())
    return result


# ---------------------------------------------------------------------------
# Step 2 — formatSlashCommandLoadingMetadata (verbatim tag format)
# ---------------------------------------------------------------------------

# constants/xml.ts
_COMMAND_NAME_TAG = "command-name"
_COMMAND_MESSAGE_TAG = "command-message"
_COMMAND_ARGS_TAG = "command-args"


def format_slash_command_loading_metadata(name: str, args: str = "") -> str:
    """Port of ``formatSlashCommandLoadingMetadata`` — the command metadata tags."""

    parts = [
        f"<{_COMMAND_MESSAGE_TAG}>{name}</{_COMMAND_MESSAGE_TAG}>",
        f"<{_COMMAND_NAME_TAG}>/{name}</{_COMMAND_NAME_TAG}>",
    ]
    if args:
        parts.append(f"<{_COMMAND_ARGS_TAG}>{args}</{_COMMAND_ARGS_TAG}>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Step 1 runtime — BotCommandRuntime (get_prompt_for_command's context)
# ---------------------------------------------------------------------------


class BotCommandRuntime(CommandRuntime):
    """``CommandRuntime`` wired to the bot's environment.

    ``run_shell`` executes in ``working_dir`` (default: the bot's cwd, i.e. the
    prod repo — same cwd semantics as free-code), so ``!`git status``` reflects
    the real repository. ``get_default_branch`` and attribution are backed by
    git / the bot's commit conventions.
    """

    def __init__(self, working_dir: Optional[str] = None) -> None:
        self.working_dir = working_dir or os.environ.get(
            "FREE_CODE_SLASH_CWD", os.getcwd()
        )

    async def run_shell(self, command: str) -> ShellResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                return ShellResult(
                    stdout="", stderr="command timed out", interrupted=True
                )
            return ShellResult(
                stdout=out.decode("utf-8", "replace"),
                stderr=err.decode("utf-8", "replace"),
                interrupted=False,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return ShellResult(stdout="", stderr=str(exc), interrupted=False)

    async def get_default_branch(self) -> str:
        """Resolve the repo's default branch via ``origin/HEAD`` (fallback master)."""

        res = await self.run_shell(
            "git rev-parse --abbrev-ref origin/HEAD 2>/dev/null | sed 's@^origin/@@'"
        )
        branch = res.stdout.strip()
        return branch or "master"

    def attribution_texts(self) -> Attribution:
        return Attribution(commit=_COMMIT_ATTRIBUTION, pr=_PR_ATTRIBUTION)


# ---------------------------------------------------------------------------
# Steps 1–5 assembled — build the model-facing turn
# ---------------------------------------------------------------------------


@dataclass
class PreparedSlashTurn:
    """The bot-side equivalent of free-code's slash-command messages + query.

    ``prompt`` is the user-turn content the model sees (metadata tags + the
    expanded template). ``allowed_tools`` is the parsed additional tool grant.
    ``progress_message`` mirrors the CLI spinner label.
    """

    prompt: str
    allowed_tools: list[str]
    progress_message: str
    command_name: str
    command_tags: str
    expanded_prompt: str
    model: Optional[str] = None
    metadata: dict = field(default_factory=dict)


def _blocks_to_text(blocks: list[ContentBlock]) -> str:
    """Join text content blocks (mirrors free-code's TextBlockParam join)."""

    return "\n\n".join(
        b.text for b in blocks if b.type == "text" and b.text is not None
    )


async def build_prompt_command_turn(
    command: Command,
    args: str,
    expanded_blocks: list[ContentBlock],
    *,
    wrap_trusted: bool = True,
) -> PreparedSlashTurn:
    """Reproduce ``getMessagesForPromptSlashCommand`` for a Discord admin turn.

    Steps (numbered as in the module docstring):
      1. expand the prompt via the command (``!`cmd``` runs in ``runtime``'s cwd);
      2. build the command metadata tags;
      3. parse ``allowed_tools``;
      4/5. assemble the user-turn ``prompt`` (tags + expanded content), optionally
         wrapped in ``<trusted_user_input>`` (the bot's admin trust boundary), to
         be handed to ``call_with_tools`` with tools enabled.
    """

    if command.type.value != "prompt":
        raise ValueError(
            f"/{command.name} is a {command.type.value} command; only prompt "
            "commands are wired to Discord (see ADR-0048)."
        )

    # Step 1 has already executed inside CommandRegistry.invoke.  The bridge only
    # receives frozen metadata plus the callback result, never the callback itself.
    expanded = _blocks_to_text(expanded_blocks)

    # Step 2 — metadata tags.
    tags = format_slash_command_loading_metadata(command.name, args.strip())

    # Step 3 — allowed tools.
    allowed_tools = parse_tool_list_from_cli(list(command.allowed_tools or []))

    # Steps 4/5 — the model-facing user turn. free-code sends metadata + isMeta
    # prompt as separate messages; the bot's single-prompt entry concatenates
    # them (tags first, then the expanded template) so the model sees the same
    # content and ordering. Wrap as trusted admin input for the bot's trust model.
    body = f"{tags}\n\n{expanded}"
    prompt = (
        f"<trusted_user_input>\n{body}\n</trusted_user_input>" if wrap_trusted else body
    )

    return PreparedSlashTurn(
        prompt=prompt,
        allowed_tools=allowed_tools,
        progress_message=command.progress_message,
        command_name=command.name,
        command_tags=tags,
        expanded_prompt=expanded,
        model=command.model,
    )

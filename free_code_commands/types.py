"""Immutable public command metadata and command result types.

Executable callbacks and runtime predicates deliberately do not live in this
module.  They are private implementation details of :mod:`free_code_commands.registry`;
``CommandRegistry.invoke`` is the only public execution boundary.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class CommandType(str, enum.Enum):
    """The ``type`` discriminator. Values match the TS string literals."""

    LOCAL = "local"
    PROMPT = "prompt"
    LOCAL_JSX = "local-jsx"


class CommandExecutionError(RuntimeError):
    """Compatibility exception for callers migrating to registry invocation."""


# --- content blocks -------------------------------------------------------
# Anthropic SDK ContentBlockParam, narrowed to what commands actually emit.


@dataclass(frozen=True)
class ContentBlock:
    """A single Anthropic content block. Prompt commands return a list of these."""

    type: str
    text: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            d["text"] = self.text
        return d


def text_block(text: str) -> ContentBlock:
    """Shorthand for ``{type: 'text', text}`` — the overwhelmingly common case."""

    return ContentBlock(type="text", text=text)


# --- local command results ------------------------------------------------
# Port of the ``LocalCommandResult`` union:
#   | { type: 'text'; value: string }
#   | { type: 'compact'; compactionResult; displayText? }
#   | { type: 'skip' }


@dataclass(frozen=True)
class LocalCommandResult:
    """Result of a ``local`` command's ``call``.

    ``type`` is one of ``"text" | "compact" | "skip"``. For ``"text"`` the payload
    is ``value``; for ``"compact"`` it is ``compaction_result`` (+ optional
    ``display_text``); ``"skip"`` carries nothing.
    """

    type: str = "text"
    value: str = ""
    compaction_result: Any = None
    display_text: Optional[str] = None

    @classmethod
    def text(cls, value: str) -> "LocalCommandResult":
        return cls(type="text", value=value)

    @classmethod
    def skip(cls) -> "LocalCommandResult":
        return cls(type="skip")

    @classmethod
    def compact(
        cls, compaction_result: Any, display_text: Optional[str] = None
    ) -> "LocalCommandResult":
        return cls(
            type="compact",
            compaction_result=compaction_result,
            display_text=display_text,
        )


@dataclass(frozen=True)
class JsxOutcome:
    """Headless outcome of a ``local-jsx`` (interactive/TUI) command.

    The upstream command renders an Ink React element and finishes by calling
    ``onDone(result, options)``. There is no terminal here, so the port performs
    any non-UI side effect through the runtime and reports what a headless caller
    needs: a human-visible ``message``, the ``display`` channel the TS ``onDone``
    would have used, and whether the model should be re-queried afterwards.
    """

    message: Optional[str] = None
    display: str = "user"  # 'skip' | 'system' | 'user'
    should_query: bool = False
    # Structured record of the dialog the TUI would have opened, so tests and
    # non-interactive front-ends can act on the intent without a terminal.
    dialog: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)


# --- public metadata-only command variants -------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Command:
    """Frozen, non-executable command metadata.

    The tuple normalization in ``__post_init__`` prevents callers from smuggling
    mutable lists into an otherwise frozen instance.
    """

    name: str
    description: str
    type: CommandType = CommandType.LOCAL
    aliases: tuple[str, ...] = ()
    availability: Optional[tuple[str, ...]] = None
    argument_hint: Optional[str] = None
    when_to_use: Optional[str] = None
    version: Optional[str] = None
    kind: Optional[str] = None
    immediate: bool = False
    is_sensitive: bool = False
    is_mcp: bool = False
    disable_model_invocation: bool = False
    user_invocable: bool = True
    supports_non_interactive: bool = False
    loaded_from: Optional[str] = None
    hidden: bool = False
    progress_message: Optional[str] = None
    content_length: Optional[int] = None
    allowed_tools: tuple[str, ...] = ()
    arg_names: Optional[tuple[str, ...]] = None
    model: Optional[str] = None
    source: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))
        if self.availability is not None:
            object.__setattr__(self, "availability", tuple(self.availability))
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        if self.arg_names is not None:
            object.__setattr__(self, "arg_names", tuple(self.arg_names))

    def user_facing_name(self) -> str:
        """Port of ``getCommandName`` — defaults to ``name``."""

        return self.name

    def matches(self, token: str) -> bool:
        """True if ``token`` is this command's name or one of its aliases."""

        return token == self.name or token in self.aliases


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalCommand(Command):
    """Metadata for a ``local`` command."""

    type: CommandType = field(default=CommandType.LOCAL, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptCommand(Command):
    """Metadata for a ``prompt`` command."""

    type: CommandType = field(default=CommandType.PROMPT, init=False)
    supports_non_interactive: bool = True
    source: Optional[str] = "builtin"


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalJsxCommand(Command):
    """Metadata for a ``local-jsx`` command."""

    type: CommandType = field(default=CommandType.LOCAL_JSX, init=False)

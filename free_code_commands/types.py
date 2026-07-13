"""Command type model — Python port of free-code's ``src/types/command.ts``.

TypeScript models a command as ``CommandBase & (PromptCommand | LocalCommand |
LocalJSXCommand)`` — a discriminated union on the ``type`` field. Python has no
structural intersection, so we express the same shape with a :class:`Command`
base dataclass carrying the shared metadata and three concrete subclasses, one
per ``type`` discriminator.

The lazy ``load()`` indirection from the TS side (deferring heavy imports until a
command runs) is dropped: Python module import is cheap and the behaviour lives
directly on the instance as ``call`` / ``get_prompt_for_command``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .runtime import CommandRuntime


class CommandType(str, enum.Enum):
    """The ``type`` discriminator. Values match the TS string literals."""

    LOCAL = "local"
    PROMPT = "prompt"
    LOCAL_JSX = "local-jsx"


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
    def compact(cls, compaction_result: Any, display_text: Optional[str] = None) -> "LocalCommandResult":
        return cls(type="compact", compaction_result=compaction_result, display_text=display_text)


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


# --- command base + variants ---------------------------------------------


@dataclass
class Command:
    """Shared metadata (``CommandBase`` in TS). Not instantiated directly."""

    name: str
    description: str
    type: CommandType = CommandType.LOCAL
    aliases: list[str] = field(default_factory=list)
    # None => available everywhere; else a subset of {'claude-ai', 'console'}.
    availability: Optional[list[str]] = None
    argument_hint: Optional[str] = None
    when_to_use: Optional[str] = None
    version: Optional[str] = None
    kind: Optional[str] = None  # 'workflow'
    immediate: bool = False
    is_sensitive: bool = False
    is_mcp: bool = False
    disable_model_invocation: bool = False
    user_invocable: bool = True
    loaded_from: Optional[str] = None
    # Static hidden flag; dynamic hiding is expressed by overriding ``hidden``.
    _is_hidden: bool = False
    # Optional predicates depending on runtime/env. When None the defaults
    # (enabled=True, hidden=_is_hidden) apply. Faithful to TS isEnabled/isHidden.
    is_enabled_fn: Optional[Callable[["CommandRuntime"], bool]] = None
    is_hidden_fn: Optional[Callable[["CommandRuntime"], bool]] = None
    # Optional dynamic description (TS uses `get description()` for a few).
    description_fn: Optional[Callable[["CommandRuntime"], str]] = None

    # ---- resolved views (take a runtime, like the TS getters read globals) --

    def enabled(self, rt: "CommandRuntime") -> bool:
        """Port of ``isCommandEnabled`` — defaults to True."""

        if self.is_enabled_fn is not None:
            return self.is_enabled_fn(rt)
        return True

    def hidden(self, rt: "CommandRuntime") -> bool:
        if self.is_hidden_fn is not None:
            return self.is_hidden_fn(rt)
        return self._is_hidden

    def resolved_description(self, rt: "CommandRuntime") -> str:
        if self.description_fn is not None:
            return self.description_fn(rt)
        return self.description

    def user_facing_name(self) -> str:
        """Port of ``getCommandName`` — defaults to ``name``."""

        return self.name

    def matches(self, token: str) -> bool:
        """True if ``token`` is this command's name or one of its aliases."""

        return token == self.name or token in self.aliases


@dataclass
class LocalCommand(Command):
    """``type: 'local'`` — computes locally and returns a :class:`LocalCommandResult`."""

    supports_non_interactive: bool = False
    # call(args, runtime) -> LocalCommandResult
    call_fn: Optional[Callable[[str, "CommandRuntime"], Any]] = None

    def __post_init__(self) -> None:
        self.type = CommandType.LOCAL

    async def call(self, args: str, rt: "CommandRuntime") -> LocalCommandResult:
        if self.call_fn is None:
            raise NotImplementedError(f"local command '{self.name}' has no call implementation")
        result = self.call_fn(args, rt)
        return await _maybe_await(result)


@dataclass
class PromptCommand(Command):
    """``type: 'prompt'`` — expands into content blocks sent to the model."""

    progress_message: str = ""
    content_length: int = 0
    allowed_tools: list[str] = field(default_factory=list)
    arg_names: Optional[list[str]] = None
    model: Optional[str] = None
    source: str = "builtin"
    disable_non_interactive: bool = False
    # get_prompt_for_command(args, runtime) -> list[ContentBlock]
    get_prompt_fn: Optional[Callable[[str, "CommandRuntime"], Any]] = None

    def __post_init__(self) -> None:
        self.type = CommandType.PROMPT

    async def get_prompt_for_command(self, args: str, rt: "CommandRuntime") -> list[ContentBlock]:
        if self.get_prompt_fn is None:
            raise NotImplementedError(f"prompt command '{self.name}' has no prompt implementation")
        result = self.get_prompt_fn(args, rt)
        return await _maybe_await(result)


@dataclass
class LocalJsxCommand(Command):
    """``type: 'local-jsx'`` — interactive/TUI command, ported headlessly.

    ``call`` performs any non-UI side effect via the runtime and returns a
    :class:`JsxOutcome` describing what the terminal dialog would have surfaced.
    """

    # call(args, runtime) -> JsxOutcome
    call_fn: Optional[Callable[[str, "CommandRuntime"], Any]] = None

    def __post_init__(self) -> None:
        self.type = CommandType.LOCAL_JSX

    async def call(self, args: str, rt: "CommandRuntime") -> JsxOutcome:
        if self.call_fn is None:
            # A pure dialog with no headless action still reports its intent.
            return JsxOutcome(dialog=self.name)
        result = self.call_fn(args, rt)
        return await _maybe_await(result)


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable, else return it as-is.

    Lets command implementations be plain ``def`` or ``async def`` interchangeably,
    the way TS command bodies are all async but many do no real awaiting.
    """

    import inspect

    if inspect.isawaitable(value):
        return await value
    return value

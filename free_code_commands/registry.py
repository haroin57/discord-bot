"""Runtime-gated command catalog and the sole command execution boundary."""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from .runtime import CommandRuntime
from .types import (
    Command,
    CommandType,
    JsxOutcome,
    LocalCommand,
    LocalJsxCommand,
    PromptCommand,
)

# ``CommandMetadata`` remains the public compatibility name.  Every public
# command value is now an immutable, metadata-only ``Command`` instance.
CommandMetadata = Command

_RuntimePredicate = Callable[[CommandRuntime], bool]
_DescriptionResolver = Callable[[CommandRuntime], str]
_Implementation = Callable[[str, CommandRuntime], Any]


def _enabled_by_default(_runtime: CommandRuntime) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class _CommandDefinition:
    """Private immutable definition containing all executable capabilities."""

    name: str
    description: str
    type: CommandType
    aliases: tuple[str, ...] = ()
    availability: tuple[str, ...] | None = None
    argument_hint: str | None = None
    when_to_use: str | None = None
    version: str | None = None
    kind: str | None = None
    immediate: bool = False
    is_sensitive: bool = False
    is_mcp: bool = False
    disable_model_invocation: bool = False
    user_invocable: bool = True
    supports_non_interactive: bool = False
    loaded_from: str | None = None
    static_hidden: bool = False
    progress_message: str | None = None
    content_length: int | None = None
    allowed_tools: tuple[str, ...] = ()
    arg_names: tuple[str, ...] | None = None
    model: str | None = None
    source: str | None = None
    _implementation: _Implementation | None = None
    _is_enabled: _RuntimePredicate = _enabled_by_default
    _is_hidden: _RuntimePredicate | None = None
    _resolve_description: _DescriptionResolver | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))
        if self.availability is not None:
            object.__setattr__(self, "availability", tuple(self.availability))
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        if self.arg_names is not None:
            object.__setattr__(self, "arg_names", tuple(self.arg_names))


def _make_definition(
    *,
    name: str,
    description: str,
    type: CommandType,
    aliases: Iterable[str] = (),
    availability: Iterable[str] | None = None,
    argument_hint: str | None = None,
    when_to_use: str | None = None,
    version: str | None = None,
    kind: str | None = None,
    immediate: bool = False,
    is_sensitive: bool = False,
    is_mcp: bool = False,
    disable_model_invocation: bool = False,
    user_invocable: bool = True,
    supports_non_interactive: bool = False,
    loaded_from: str | None = None,
    _is_hidden: bool = False,
    progress_message: str | None = None,
    content_length: int | None = None,
    allowed_tools: Iterable[str] = (),
    arg_names: Iterable[str] | None = None,
    model: str | None = None,
    source: str | None = None,
    implementation: _Implementation | None = None,
    is_enabled_fn: _RuntimePredicate | None = None,
    is_hidden_fn: _RuntimePredicate | None = None,
    description_fn: _DescriptionResolver | None = None,
) -> _CommandDefinition:
    return _CommandDefinition(
        name=name,
        description=description,
        type=type,
        aliases=tuple(aliases),
        availability=tuple(availability) if availability is not None else None,
        argument_hint=argument_hint,
        when_to_use=when_to_use,
        version=version,
        kind=kind,
        immediate=immediate,
        is_sensitive=is_sensitive,
        is_mcp=is_mcp,
        disable_model_invocation=disable_model_invocation,
        user_invocable=user_invocable,
        supports_non_interactive=supports_non_interactive,
        loaded_from=loaded_from,
        static_hidden=_is_hidden,
        progress_message=progress_message,
        content_length=content_length,
        allowed_tools=tuple(allowed_tools),
        arg_names=tuple(arg_names) if arg_names is not None else None,
        model=model,
        source=source,
        _implementation=implementation,
        _is_enabled=is_enabled_fn or _enabled_by_default,
        _is_hidden=is_hidden_fn,
        _resolve_description=description_fn,
    )


def _local_command(
    *,
    call_fn: _Implementation | None = None,
    **metadata: Any,
) -> _CommandDefinition:
    """Private builder used only by built-in command definition modules."""

    return _make_definition(
        type=CommandType.LOCAL,
        implementation=call_fn,
        **metadata,
    )


def _prompt_command(
    *,
    get_prompt_fn: _Implementation | None = None,
    supports_non_interactive: bool = True,
    source: str | None = "builtin",
    **metadata: Any,
) -> _CommandDefinition:
    """Private builder used only by built-in command definition modules."""

    return _make_definition(
        type=CommandType.PROMPT,
        implementation=get_prompt_fn,
        supports_non_interactive=supports_non_interactive,
        source=source,
        **metadata,
    )


def _local_jsx_command(
    *,
    call_fn: _Implementation | None = None,
    **metadata: Any,
) -> _CommandDefinition:
    """Private builder used only by built-in command definition modules."""

    return _make_definition(
        type=CommandType.LOCAL_JSX,
        implementation=call_fn,
        **metadata,
    )


def _metadata_for(
    definition: _CommandDefinition,
    *,
    runtime: CommandRuntime | None = None,
    aliases: tuple[str, ...] | None = None,
) -> CommandMetadata:
    metadata_type: type[Command]
    if definition.type is CommandType.LOCAL:
        metadata_type = LocalCommand
    elif definition.type is CommandType.PROMPT:
        metadata_type = PromptCommand
    else:
        metadata_type = LocalJsxCommand
    return metadata_type(
        name=definition.name,
        description=(
            definition._resolve_description(runtime)
            if runtime is not None and definition._resolve_description is not None
            else definition.description
        ),
        aliases=definition.aliases if aliases is None else aliases,
        availability=definition.availability,
        argument_hint=definition.argument_hint,
        when_to_use=definition.when_to_use,
        version=definition.version,
        kind=definition.kind,
        immediate=definition.immediate,
        is_sensitive=definition.is_sensitive,
        is_mcp=definition.is_mcp,
        disable_model_invocation=definition.disable_model_invocation,
        user_invocable=definition.user_invocable,
        supports_non_interactive=definition.supports_non_interactive,
        loaded_from=definition.loaded_from,
        hidden=(
            definition._is_hidden(runtime)
            if runtime is not None and definition._is_hidden is not None
            else definition.static_hidden
        ),
        progress_message=definition.progress_message,
        content_length=definition.content_length,
        allowed_tools=definition.allowed_tools,
        arg_names=definition.arg_names,
        model=definition.model,
        source=definition.source,
    )


def _metadata_catalog(
    definitions: Iterable[_CommandDefinition],
) -> tuple[CommandMetadata, ...]:
    """Project private definitions to an immutable public module catalog."""

    return tuple(_metadata_for(definition) for definition in definitions)


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """Result of one command executed through :meth:`CommandRegistry.invoke`."""

    command: CommandMetadata
    args: str
    result: Any


# An adapter receives exactly metadata and the already-executed result. Parsed
# arguments and the capability-bearing runtime remain inside ``invoke``.
InvocationExecutor = Callable[[CommandMetadata, Any], Any]


class CommandRegistry:
    """Expose immutable metadata while retaining capabilities in private storage."""

    def __init__(self) -> None:
        self.__definitions: tuple[_CommandDefinition, ...] = ()

    @classmethod
    def _from_definitions(
        cls,
        definitions: Iterable[_CommandDefinition],
    ) -> CommandRegistry:
        registry = cls()
        # Clone each frozen value so separately built registries do not even share
        # definition identities.  Tuple-valued metadata prevents nested mutation.
        registry.__definitions = tuple(
            replace(definition) for definition in definitions
        )
        return registry

    def all(self) -> list[CommandMetadata]:
        """Return non-executable metadata for every registered command."""

        return [_metadata_for(definition) for definition in self.__definitions]

    def __len__(self) -> int:
        return len(self.__definitions)

    def __iter__(self):
        return iter(self.all())

    def find(self, name: str) -> CommandMetadata | None:
        """Return immutable metadata without applying runtime invocation gates."""

        definition = self.__find_definition(name)
        return _metadata_for(definition) if definition is not None else None

    def has(self, name: str) -> bool:
        return self.__find_definition(name) is not None

    def get(self, name: str) -> CommandMetadata:
        metadata = self.find(name)
        if metadata is not None:
            return metadata
        available = sorted(
            (
                f"{definition.name} (aliases: {', '.join(definition.aliases)})"
                if definition.aliases
                else definition.name
            )
            for definition in self.__definitions
        )
        raise KeyError(
            f"Command {name} not found. Available commands: {', '.join(available)}"
        )

    def __find_definition(self, name: str) -> _CommandDefinition | None:
        for definition in self.__definitions:
            if definition.name == name:
                return definition
        for definition in self.__definitions:
            if name in definition.aliases:
                return definition
        return None

    def meets_availability(
        self,
        command: CommandMetadata,
        runtime: CommandRuntime,
    ) -> bool:
        """Apply availability constraints using public metadata only."""

        if not command.availability:
            return True
        for availability in command.availability:
            if availability == "claude-ai" and runtime.is_claude_ai_subscriber():
                return True
            if availability == "console" and (
                not runtime.is_claude_ai_subscriber()
                and not runtime.is_using_3p_services()
                and runtime.has_anthropic_api_key_auth()
            ):
                return True
        return False

    def active(
        self,
        runtime: CommandRuntime,
        *,
        include_hidden: bool = True,
    ) -> list[CommandMetadata]:
        entries = self.__active_entries(runtime)
        if include_hidden:
            return [metadata for _, metadata in entries]
        return [metadata for _, metadata in entries if not metadata.hidden]

    def __dispatchable(
        self,
        definition: _CommandDefinition,
        runtime: CommandRuntime,
    ) -> bool:
        if not definition.user_invocable or not definition._is_enabled(runtime):
            return False
        if not self.meets_availability(_metadata_for(definition), runtime):
            return False
        return not (
            runtime.is_non_interactive() and not definition.supports_non_interactive
        )

    def __active_entries(
        self,
        runtime: CommandRuntime,
    ) -> list[tuple[_CommandDefinition, CommandMetadata]]:
        definitions = [
            definition
            for definition in self.__definitions
            if self.__dispatchable(definition, runtime)
        ]
        canonical_names = [definition.name for definition in definitions]
        duplicates = sorted(
            name for name, count in Counter(canonical_names).items() if count > 1
        )
        if duplicates:
            raise ValueError(
                "active command catalog has duplicate canonical names: "
                f"{', '.join(duplicates)}"
            )

        claimed = set(canonical_names)
        entries: list[tuple[_CommandDefinition, CommandMetadata]] = []
        for definition in definitions:
            aliases: list[str] = []
            for alias in definition.aliases:
                if alias in claimed:
                    continue
                claimed.add(alias)
                aliases.append(alias)
            entries.append(
                (
                    definition,
                    _metadata_for(
                        definition,
                        runtime=runtime,
                        aliases=tuple(aliases),
                    ),
                )
            )
        return entries

    def resolve(self, name: str, runtime: CommandRuntime) -> CommandMetadata | None:
        entry = self.__resolve_entry(name, runtime)
        return entry[1] if entry is not None else None

    def dispatch(
        self,
        text: str,
        runtime: CommandRuntime,
    ) -> tuple[CommandMetadata | None, str]:
        name, args = self._split(text)
        if name is None:
            return None, ""
        return self.resolve(name, runtime), args

    def parse(
        self,
        text: str,
        runtime: CommandRuntime,
    ) -> tuple[CommandMetadata | None, str]:
        return self.dispatch(text, runtime)

    async def invoke(
        self,
        text: str,
        runtime: CommandRuntime,
        *,
        executor: InvocationExecutor | None = None,
    ) -> CommandInvocation | None:
        """Resolve gates and execute without exposing the implementation object."""

        name, args = self._split(text)
        if name is None:
            return None
        entry = self.__resolve_entry(name, runtime)
        if entry is None:
            return None
        definition, metadata = entry

        if definition._implementation is None:
            if definition.type is CommandType.LOCAL_JSX:
                result: Any = JsxOutcome(dialog=definition.name)
            else:
                raise NotImplementedError(
                    f"command '{definition.name}' has no implementation"
                )
        else:
            result = definition._implementation(args, runtime)
            if inspect.isawaitable(result):
                result = await result

        if executor is not None:
            result = executor(metadata, result)
            if inspect.isawaitable(result):
                result = await result

        if isinstance(result, _CommandDefinition):
            raise TypeError("command execution must not return private definitions")
        return CommandInvocation(command=metadata, args=args, result=result)

    def __resolve_entry(
        self,
        name: str,
        runtime: CommandRuntime,
    ) -> tuple[_CommandDefinition, CommandMetadata] | None:
        entries = self.__active_entries(runtime)
        for definition, metadata in entries:
            if definition.name == name:
                return definition, metadata
        for definition, metadata in entries:
            if name in metadata.aliases:
                return definition, metadata
        return None

    @staticmethod
    def _split(text: str) -> tuple[str | None, str]:
        if not text:
            return None, ""
        stripped = text.lstrip()
        if stripped.startswith("/"):
            stripped = stripped[1:]
        if not stripped:
            return None, ""
        parts = stripped.split(None, 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

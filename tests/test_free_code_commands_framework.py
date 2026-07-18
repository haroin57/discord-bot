"""PoC: framework core, shell interpolation, registry, and enumeration parity.

Verifies that the Python port of free-code's slash-command system reproduces the
upstream contract: the three command flavours dispatch, ``!`cmd``` / ``$ARGUMENTS``
expansion behaves like ``promptShellExecution.ts``, the registry resolves names +
aliases + availability like ``commands.ts``, and every registered upstream command
is accounted for.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from free_code_commands import CommandMetadata, build_registry
from free_code_commands.registry import CommandRegistry
from free_code_commands.runtime import RecordingRuntime, ShellResult
from free_code_commands.shell import (
    MalformedCommandError,
    execute_shell_commands_in_prompt,
    substitute_arguments,
)
from free_code_commands.types import (
    CommandType,
    JsxOutcome,
    LocalCommand,
    LocalCommandResult,
    LocalJsxCommand,
    PromptCommand,
    text_block,
)
from free_code_commands._all import all_commands, commands_by_module


# --------------------------------------------------------------------------
# Command type model
# --------------------------------------------------------------------------


def test_command_type_discriminators():
    lc = LocalCommand(name="a", description="d")
    pc = PromptCommand(name="b", description="d")
    jc = LocalJsxCommand(name="c", description="d")
    assert lc.type is CommandType.LOCAL
    assert pc.type is CommandType.PROMPT
    assert jc.type is CommandType.LOCAL_JSX
    # user_facing_name defaults to name
    assert lc.user_facing_name() == "a"


def test_local_command_result_constructors():
    assert LocalCommandResult.text("hi").type == "text"
    assert LocalCommandResult.text("hi").value == "hi"
    assert LocalCommandResult.skip().type == "skip"
    r = LocalCommandResult.compact({"summary": "s"}, display_text="d")
    assert (
        r.type == "compact"
        and r.compaction_result == {"summary": "s"}
        and r.display_text == "d"
    )


def test_text_block_to_dict():
    b = text_block("hello")
    assert b.type == "text"
    assert b.to_dict() == {"type": "text", "text": "hello"}


def test_public_command_types_reject_executable_capabilities():
    with pytest.raises(TypeError):
        LocalCommand(
            name="x",
            description="d",
            call_fn=lambda _args, _runtime: LocalCommandResult.skip(),
        )
    with pytest.raises(TypeError):
        PromptCommand(
            name="x",
            description="d",
            get_prompt_fn=lambda _args, _runtime: [text_block("x")],
        )


async def test_registry_invokes_sync_and_async_implementations():
    registry = build_registry()
    sync_result = await registry.invoke("/theme", RecordingRuntime())
    async_result = await registry.invoke("/cost", RecordingRuntime())
    assert isinstance(sync_result.result, JsxOutcome)
    assert sync_result.result.dialog == "theme"
    assert async_result.result.type == "text"


def test_runtime_projection_resolves_predicates_without_exposing_them():
    registry = build_registry()
    ant = registry.resolve(
        "model",
        RecordingRuntime(user_type="ant", main_loop_model="opus"),
    )
    assert ant.description == "Set the AI model for Claude Code (currently opus)"
    assert not hasattr(ant, "description_fn")
    assert not hasattr(ant, "is_enabled_fn")


# --------------------------------------------------------------------------
# Shell interpolation (promptShellExecution.ts)
# --------------------------------------------------------------------------


async def test_inline_shell_expansion():
    rt = RecordingRuntime(
        shell_outputs={"git status": ShellResult(stdout="On branch main\n")}
    )
    out = await execute_shell_commands_in_prompt("status: !`git status`", rt, "/t")
    assert out == "status: On branch main"
    assert rt.shell_calls == ["git status"]


async def test_inline_expansion_preserves_boundary_char():
    rt = RecordingRuntime(shell_outputs={"echo hi": ShellResult(stdout="hi")})
    # `!` must be preceded by whitespace/start-of-line; the boundary space is kept.
    out = await execute_shell_commands_in_prompt("a !`echo hi` b", rt, "/t")
    assert out == "a hi b"


async def test_block_shell_expansion():
    rt = RecordingRuntime(shell_outputs={"ls": ShellResult(stdout="file.txt")})
    text = "before\n```!\nls\n```\nafter"
    out = await execute_shell_commands_in_prompt(text, rt, "/t")
    assert "file.txt" in out
    assert rt.shell_calls == ["ls"]


async def test_no_inline_marker_means_no_shell_scan():
    rt = RecordingRuntime()
    out = await execute_shell_commands_in_prompt("no commands here", rt, "/t")
    assert out == "no commands here"
    assert rt.shell_calls == []


async def test_shell_permission_denied_raises():
    class Denier(RecordingRuntime):
        def can_run_shell(self, command: str) -> bool:
            return False

    with pytest.raises(MalformedCommandError):
        await execute_shell_commands_in_prompt("x !`rm -rf /`", Denier(), "/t")


def test_substitute_arguments_forms():
    assert substitute_arguments("run $ARGUMENTS now", "a b c") == "run a b c now"
    assert substitute_arguments("first=$1 second=$2", "x y") == "first=x second=y"
    # named placeholders
    assert substitute_arguments("$FOO/$BAR", "1 2", arg_names=["FOO", "BAR"]) == "1/2"
    # no placeholder → args appended
    assert substitute_arguments("template", "extra") == "template\n\nextra"
    # no placeholder + no args → unchanged
    assert substitute_arguments("template", "") == "template"


# --------------------------------------------------------------------------
# Registry (commands.ts findCommand / getCommand / meetsAvailabilityRequirement)
# --------------------------------------------------------------------------


@pytest.fixture
def registry() -> CommandRegistry:
    return build_registry()


def test_registry_builds_and_has_expected_size(registry):
    # 9 prompt + 19 local + 51 jsx + 12 feature-gated + 21 internal
    assert len(registry) == 112
    assert len(all_commands()) == 112


def test_all_command_names_non_empty(registry):
    for c in registry:
        assert c.name and isinstance(c.name, str)
        assert c.description and isinstance(c.description, str)


def test_only_intended_duplicate_names(registry):
    from collections import Counter

    counts = Counter(c.name for c in registry)
    dups = {n for n, k in counts.items() if k > 1}
    # The interactive/non-interactive twins are the only legitimate duplicates.
    assert dups == {"context", "extra-usage"}


def test_alias_resolution(registry):
    cases = {
        "reset": "clear",
        "new": "clear",
        "quit": "exit",
        "bug": "feedback",
        "settings": "config",
        "app": "desktop",
        "continue": "resume",
        "allowed-tools": "permissions",
        "bashes": "tasks",
        "checkpoint": "rewind",
        "ios": "mobile",
        "android": "mobile",
        "plugins": "plugin",
        "marketplace": "plugin",
        "remote": "session",
        "rc": "remote-control",
    }
    for alias, target in cases.items():
        cmd = registry.find(alias)
        assert cmd is not None, f"alias {alias!r} did not resolve"
        assert cmd.name == target, (
            f"alias {alias!r} -> {cmd.name!r}, expected {target!r}"
        )


def test_get_unknown_raises_with_listing(registry):
    with pytest.raises(KeyError) as exc:
        registry.get("definitely-not-a-command")
    assert "not found" in str(exc.value)


def test_availability_gating(registry):
    # /usage is claude-ai only.
    usage = registry.find("usage")
    assert usage.availability == ("claude-ai",)
    assert not registry.meets_availability(usage, RecordingRuntime())
    assert registry.meets_availability(usage, RecordingRuntime(claude_ai=True))
    # install-github-app allows console (api-key) users too.
    gh = registry.find("install-github-app")
    assert registry.meets_availability(gh, RecordingRuntime(api_key_auth=True))


@pytest.mark.parametrize(
    "backend_env",
    [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ],
)
def test_console_availability_excludes_3p_backends(registry, backend_env):
    github_app = registry.get("install-github-app")
    runtime = RecordingRuntime(api_key_auth=True, env={backend_env: "1"})

    assert not registry.meets_availability(github_app, runtime)
    assert registry.resolve("install-github-app", runtime) is None


def test_active_vs_visible_counts(registry):
    rt = RecordingRuntime()
    active = registry.active(rt)
    visible = registry.active(rt, include_hidden=False)
    assert len(visible) < len(active)  # hidden commands exist (heapdump, etc.)
    assert all(not c.hidden for c in visible)


def test_twin_resolution_by_interactivity(registry):
    interactive = RecordingRuntime(non_interactive=False)
    non_interactive = RecordingRuntime(non_interactive=True)
    assert registry.resolve("context", interactive).type is CommandType.LOCAL_JSX
    assert registry.resolve("context", non_interactive).type is CommandType.LOCAL


def test_ant_unlocks_more_commands(registry):
    plain = len(registry.active(RecordingRuntime()))
    ant = len(registry.active(RecordingRuntime(user_type="ant")))
    assert ant > plain  # version, files, tag, bridge-kick unlock under ant


def test_parse_splits_command_and_args(registry):
    rt = RecordingRuntime()
    cmd, args = registry.parse("/commit-push-pr make it quick", rt)
    assert cmd is not None and cmd.name == "commit-push-pr"
    assert args == "make it quick"
    # leading slash optional; alias resolves
    assert registry.parse("reset", rt)[0].name == "clear"
    # unknown / empty
    assert registry.parse("/nope", rt)[0] is None
    assert registry.parse("", rt)[0] is None


def test_find_is_introspection_but_dispatch_enforces_runtime_gates(registry):
    plain = RecordingRuntime()
    subscriber = RecordingRuntime(claude_ai=True)

    assert registry.find("usage") is not None
    assert registry.dispatch("/usage", plain)[0] is None
    assert registry.dispatch("/usage", subscriber)[0].name == "usage"

    assert registry.find("rc").name == "remote-control"
    assert registry.dispatch("/rc", plain)[0] is None


def test_dispatch_rejects_non_user_invocable_commands():
    registry = build_registry()
    assert registry.find("backfill-sessions").name == "backfill-sessions"
    assert registry.dispatch("/backfill-sessions", RecordingRuntime())[0] is None


def test_fork_alias_changes_owner_with_feature_gate(registry):
    disabled = RecordingRuntime()
    enabled = RecordingRuntime(features={"FORK_SUBAGENT"})

    assert registry.find("fork").name == "fork"
    assert registry.dispatch("/fork", disabled)[0].name == "branch"
    assert registry.dispatch("/fork", enabled)[0].name == "fork"

    disabled_branch = next(c for c in registry.active(disabled) if c.name == "branch")
    enabled_branch = next(c for c in registry.active(enabled) if c.name == "branch")
    assert "fork" in disabled_branch.aliases
    assert "fork" not in enabled_branch.aliases


@pytest.mark.parametrize(
    "runtime",
    [RecordingRuntime(), RecordingRuntime(features={"FORK_SUBAGENT"})],
)
def test_active_catalog_names_and_aliases_are_unique(registry, runtime):
    catalog = registry.active(runtime)
    identifiers = [
        identifier
        for command in catalog
        for identifier in (command.name, *command.aliases)
    ]
    assert len(identifiers) == len(set(identifiers))


def test_login_logout_are_not_dispatchable_on_3p_services(registry):
    third_party = RecordingRuntime(env={"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert registry.find("login") is not None
    assert registry.find("logout") is not None
    assert registry.dispatch("/login", third_party)[0] is None
    assert registry.dispatch("/logout", third_party)[0] is None


async def test_lookup_returns_metadata_and_direct_call_cannot_bypass_gates(registry):
    from free_code_commands.commands import jsx_commands, prompt_commands

    third_party = RecordingRuntime(env={"CLAUDE_CODE_USE_BEDROCK": "1"})
    metadata = registry.get("login")
    raw_login = next(
        command for command in jsx_commands.COMMANDS if command.name == "login"
    )
    raw_commit = next(
        command for command in prompt_commands.COMMANDS if command.name == "commit"
    )

    assert isinstance(metadata, CommandMetadata)
    assert registry.find("login") == metadata
    assert registry.has("login") is True
    exposed_metadata = [
        metadata,
        registry.resolve("login", RecordingRuntime()),
        registry.active(RecordingRuntime())[0],
        registry.all()[0],
    ]
    assert all(isinstance(item, CommandMetadata) for item in exposed_metadata)
    assert all(not hasattr(item, "call") for item in exposed_metadata)
    assert all(not hasattr(item, "get_prompt_for_command") for item in exposed_metadata)
    assert all(not hasattr(item, "call_fn") for item in exposed_metadata)
    assert all(not hasattr(item, "get_prompt_fn") for item in exposed_metadata)
    assert all(not hasattr(item, "is_enabled_fn") for item in exposed_metadata)
    assert await registry.invoke("/login", third_party) is None
    assert not hasattr(raw_login, "call")
    assert not hasattr(raw_login, "call_fn")
    assert not hasattr(raw_commit, "get_prompt_for_command")
    assert not hasattr(raw_commit, "get_prompt_fn")
    assert third_party.opened_dialogs == []


def test_public_commands_are_frozen_and_cannot_pollute_later_registries():
    from free_code_commands.commands import jsx_commands

    raw_login = next(
        command for command in jsx_commands.COMMANDS if command.name == "login"
    )
    with pytest.raises(FrozenInstanceError):
        raw_login.hidden = False
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        raw_login.is_enabled_fn = None
    with pytest.raises(FrozenInstanceError):
        raw_login.aliases = ("poisoned",)
    assert isinstance(raw_login.aliases, tuple)

    later = build_registry()
    third_party = RecordingRuntime(env={"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert later.resolve("login", third_party) is None
    assert later.find("poisoned") is None


async def test_executor_receives_metadata_and_post_execution_result_only():
    observed = {}

    def executor(metadata, result):
        observed.update(
            metadata=metadata,
            result=result,
        )
        return result

    runtime = RecordingRuntime()
    invocation = await build_registry().invoke(
        "/cost executor-private-args",
        runtime,
        executor=executor,
    )
    assert invocation is not None
    assert set(observed) == {"metadata", "result"}
    assert isinstance(observed["metadata"], CommandMetadata)
    assert observed["result"].type == "text"
    assert not hasattr(observed["metadata"], "call_fn")
    assert invocation.args == "executor-private-args"


@pytest.mark.parametrize(
    "command_text",
    ["/rewind", "/branch", "/login", "/statusline"],
)
async def test_non_interactive_runtime_rejects_unapproved_commands(
    registry,
    command_text,
):
    assert (
        await registry.invoke(
            command_text,
            RecordingRuntime(non_interactive=True),
        )
        is None
    )


async def test_non_interactive_runtime_invokes_explicitly_supported_command(registry):
    invocation = await registry.invoke(
        "/cost",
        RecordingRuntime(non_interactive=True),
    )
    assert invocation is not None
    assert invocation.command.name == "cost"


async def test_non_interactive_runtime_invokes_supported_prompt_command(registry):
    invocation = await registry.invoke(
        "/init-verifiers",
        RecordingRuntime(non_interactive=True),
    )
    assert invocation is not None
    assert invocation.command.type is CommandType.PROMPT


# --------------------------------------------------------------------------
# Enumeration parity vs commands.ts (洗い出し網羅性)
# --------------------------------------------------------------------------

# Canonical names of the always-registered COMMANDS array in commands.ts
# (feature-gated + internal handled separately below).
_MAIN_REGISTERED = {
    "add-dir",
    "advisor",
    "agents",
    "branch",
    "btw",
    "chrome",
    "clear",
    "color",
    "compact",
    "config",
    "copy",
    "desktop",
    "context",
    "cost",
    "diff",
    "doctor",
    "effort",
    "exit",
    "fast",
    "files",
    "heapdump",
    "help",
    "ide",
    "init",
    "keybindings",
    "install-github-app",
    "install-slack-app",
    "mcp",
    "memory",
    "mobile",
    "model",
    "output-style",
    "remote-env",
    "plugin",
    "pr-comments",
    "release-notes",
    "reload-plugins",
    "rename",
    "resume",
    "session",
    "skills",
    "stats",
    "status",
    "statusline",
    "stickers",
    "tag",
    "theme",
    "feedback",
    "review",
    "ultrareview",
    "rewind",
    "security-review",
    "terminal-setup",
    "upgrade",
    "extra-usage",
    "rate-limit-options",
    "usage",
    "insights",
    "vim",
    "think-back",
    "thinkback-play",
    "permissions",
    "plan",
    "privacy-settings",
    "hooks",
    "export",
    "sandbox",
    "logout",
    "login",
    "passes",
    "tasks",
}


def test_all_main_registered_commands_present(registry):
    missing = {n for n in _MAIN_REGISTERED if registry.find(n) is None}
    assert not missing, f"missing ported commands: {sorted(missing)}"


def test_feature_gated_present(registry):
    for name in (
        "web-setup",
        "remote-control",
        "brief",
        "ultraplan",
        "fork",
        "buddy",
        "peers",
        "workflows",
        "assistant",
        "proactive",
        "torch",
        "remote-control-server",
    ):
        assert registry.find(name) is not None, f"feature-gated {name} missing"


def test_internal_present(registry):
    for name in (
        "backfill-sessions",
        "break-cache",
        "bughunter",
        "ctx_viz",
        "good-claude",
        "issue",
        "mock-limits",
        "reset-limits",
        "onboarding",
        "share",
        "summary",
        "teleport",
        "ant-trace",
        "perf-issue",
        "env",
        "oauth-refresh",
        "debug-tool-call",
        "agents-platform",
        "autofix-pr",
        "force-snip",
        "subscribe-pr",
    ):
        assert registry.find(name) is not None, f"internal {name} missing"


def test_module_grouping_counts():
    groups = commands_by_module()
    assert len(groups["prompt"]) == 9
    assert len(groups["local"]) == 19
    assert len(groups["local-jsx"]) == 51
    assert len(groups["feature-gated"]) == 12
    assert len(groups["internal"]) == 21

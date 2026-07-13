"""PoC: framework core, shell interpolation, registry, and enumeration parity.

Verifies that the Python port of free-code's slash-command system reproduces the
upstream contract: the three command flavours dispatch, ``!`cmd``` / ``$ARGUMENTS``
expansion behaves like ``promptShellExecution.ts``, the registry resolves names +
aliases + availability like ``commands.ts``, and every registered upstream command
is accounted for.
"""

from __future__ import annotations

import pytest

from free_code_commands import build_registry
from free_code_commands.registry import CommandRegistry
from free_code_commands.runtime import Attribution, CommandRuntime, RecordingRuntime, ShellResult
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
    lc = LocalCommand(name="a", description="d", call_fn=lambda args, rt: LocalCommandResult.text("x"))
    pc = PromptCommand(name="b", description="d", get_prompt_fn=lambda args, rt: [text_block("x")])
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
    assert r.type == "compact" and r.compaction_result == {"summary": "s"} and r.display_text == "d"


def test_text_block_to_dict():
    b = text_block("hello")
    assert b.type == "text"
    assert b.to_dict() == {"type": "text", "text": "hello"}


async def test_maybe_await_accepts_sync_and_async_bodies():
    sync_cmd = LocalCommand(name="s", description="d", call_fn=lambda args, rt: LocalCommandResult.text("sync"))

    async def _async_body(args, rt):
        return LocalCommandResult.text("async")

    async_cmd = LocalCommand(name="a", description="d", call_fn=_async_body)
    rt = RecordingRuntime()
    assert (await sync_cmd.call("", rt)).value == "sync"
    assert (await async_cmd.call("", rt)).value == "async"


async def test_localjsx_default_call_reports_dialog():
    jc = LocalJsxCommand(name="theme", description="d")  # no call_fn
    out = await jc.call("", RecordingRuntime())
    assert isinstance(out, JsxOutcome)
    assert out.dialog == "theme"


def test_enabled_hidden_description_predicates():
    cmd = LocalCommand(
        name="x",
        description="static",
        is_enabled_fn=lambda rt: rt.is_ant(),
        is_hidden_fn=lambda rt: not rt.is_ant(),
        description_fn=lambda rt: f"model={rt.main_loop_model()}",
    )
    ant = RecordingRuntime(user_type="ant", main_loop_model="opus")
    plain = RecordingRuntime()
    assert cmd.enabled(ant) and not cmd.hidden(ant)
    assert not cmd.enabled(plain) and cmd.hidden(plain)
    assert cmd.resolved_description(ant) == "model=opus"


# --------------------------------------------------------------------------
# Shell interpolation (promptShellExecution.ts)
# --------------------------------------------------------------------------


async def test_inline_shell_expansion():
    rt = RecordingRuntime(shell_outputs={"git status": ShellResult(stdout="On branch main\n")})
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
        assert cmd.name == target, f"alias {alias!r} -> {cmd.name!r}, expected {target!r}"


def test_get_unknown_raises_with_listing(registry):
    with pytest.raises(KeyError) as exc:
        registry.get("definitely-not-a-command")
    assert "not found" in str(exc.value)


def test_availability_gating(registry):
    # /usage is claude-ai only.
    usage = registry.find("usage")
    assert usage.availability == ["claude-ai"]
    assert not registry.meets_availability(usage, RecordingRuntime())
    assert registry.meets_availability(usage, RecordingRuntime(claude_ai=True))
    # install-github-app allows console (api-key) users too.
    gh = registry.find("install-github-app")
    assert registry.meets_availability(gh, RecordingRuntime(api_key_auth=True))


def test_active_vs_visible_counts(registry):
    rt = RecordingRuntime()
    active = registry.active(rt)
    visible = registry.active(rt, include_hidden=False)
    assert len(visible) < len(active)  # hidden commands exist (heapdump, etc.)
    assert all(not c.hidden(rt) for c in visible)


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
    cmd, args = registry.parse("/commit-push-pr make it quick")
    assert cmd is not None and cmd.name == "commit-push-pr"
    assert args == "make it quick"
    # leading slash optional; alias resolves
    assert registry.parse("reset")[0].name == "clear"
    # unknown / empty
    assert registry.parse("/nope")[0] is None
    assert registry.parse("")[0] is None


# --------------------------------------------------------------------------
# Enumeration parity vs commands.ts (洗い出し網羅性)
# --------------------------------------------------------------------------

# Canonical names of the always-registered COMMANDS array in commands.ts
# (feature-gated + internal handled separately below).
_MAIN_REGISTERED = {
    "add-dir", "advisor", "agents", "branch", "btw", "chrome", "clear", "color",
    "compact", "config", "copy", "desktop", "context", "cost", "diff", "doctor",
    "effort", "exit", "fast", "files", "heapdump", "help", "ide", "init",
    "keybindings", "install-github-app", "install-slack-app", "mcp", "memory",
    "mobile", "model", "output-style", "remote-env", "plugin", "pr-comments",
    "release-notes", "reload-plugins", "rename", "resume", "session", "skills",
    "stats", "status", "statusline", "stickers", "tag", "theme", "feedback",
    "review", "ultrareview", "rewind", "security-review", "terminal-setup",
    "upgrade", "extra-usage", "rate-limit-options", "usage", "insights", "vim",
    "think-back", "thinkback-play", "permissions", "plan", "privacy-settings",
    "hooks", "export", "sandbox", "logout", "login", "passes", "tasks",
}


def test_all_main_registered_commands_present(registry):
    missing = {n for n in _MAIN_REGISTERED if registry.find(n) is None}
    assert not missing, f"missing ported commands: {sorted(missing)}"


def test_feature_gated_present(registry):
    for name in (
        "web-setup", "remote-control", "brief", "ultraplan", "fork", "buddy",
        "peers", "workflows", "assistant", "proactive", "torch",
        "remote-control-server",
    ):
        assert registry.find(name) is not None, f"feature-gated {name} missing"


def test_internal_present(registry):
    for name in (
        "backfill-sessions", "break-cache", "bughunter", "ctx_viz", "good-claude",
        "issue", "mock-limits", "reset-limits", "onboarding", "share", "summary",
        "teleport", "ant-trace", "perf-issue", "env", "oauth-refresh",
        "debug-tool-call", "agents-platform", "autofix-pr", "force-snip",
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

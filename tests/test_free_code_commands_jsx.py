"""PoC: local-jsx (interactive) commands + feature-gated commands.

The TUI is headless here, so registry invocation must record which dialog it
would open and return a :class:`JsxOutcome`. Also verifies dynamic descriptions,
per-runtime enablement/hiding gates, and that feature-gated commands stay dark
until their flag is set — matching the conditional registration in ``commands.ts``.
"""

from __future__ import annotations

from free_code_commands import build_registry
from free_code_commands.runtime import RecordingRuntime
from free_code_commands.types import CommandType, JsxOutcome


def _resolved(name, runtime):
    return build_registry().resolve(name, runtime)


# --------------------------------------------------------------------------
# Headless dialog contract
# --------------------------------------------------------------------------


async def test_every_jsx_command_opens_its_dialog():
    registry = build_registry()
    catalog_runtime = RecordingRuntime()
    active = [
        command
        for command in registry.active(catalog_runtime)
        if command.type is CommandType.LOCAL_JSX
    ]
    for command in active:
        rt = RecordingRuntime()
        invocation = await registry.invoke(f"/{command.name} some args", rt)
        assert invocation is not None
        out = invocation.result
        assert isinstance(out, JsxOutcome)
        assert out.dialog == command.name
        assert rt.opened_dialogs and rt.opened_dialogs[0][0] == command.name
        # args are threaded into the recorded dialog payload
        assert rt.opened_dialogs[0][1].get("args") == "some args"


# --------------------------------------------------------------------------
# Dynamic descriptions (get description() in the TS)
# --------------------------------------------------------------------------


def test_model_description_shows_current_model():
    desc = _resolved(
        "model",
        RecordingRuntime(main_loop_model="opus-4.8"),
    ).description
    assert "currently opus-4.8" in desc


def test_fast_description_shows_fast_model():
    class FastRt(RecordingRuntime):
        def fast_mode_model_display(self):
            return "Haiku"

    assert (
        "Haiku only"
        in _resolved(
            "fast",
            FastRt(fast_mode=True, claude_ai=True),
        ).description
    )


def test_login_description_switches_on_auth():
    signin = _resolved("login", RecordingRuntime()).description
    switch = _resolved("login", RecordingRuntime(api_key_auth=True)).description
    assert signin == "Sign in with your Anthropic account"
    assert switch == "Switch Anthropic accounts"


def test_terminal_setup_description_and_hiding():
    apple = _resolved(
        "terminal-setup",
        RecordingRuntime(terminal="Apple_Terminal"),
    ).description
    other = _resolved(
        "terminal-setup",
        RecordingRuntime(terminal="xterm"),
    ).description
    assert "Option+Enter" in apple
    assert "Shift+Enter" in other
    # hidden on native CSI-u terminals
    assert _resolved("terminal-setup", RecordingRuntime(terminal="kitty")).hidden
    assert not _resolved("terminal-setup", RecordingRuntime(terminal="xterm")).hidden


def test_sandbox_description_is_dynamic():
    class SandRt(RecordingRuntime):
        def sandbox_status(self):
            return "sandbox enabled (auto-allow)"

    assert "auto-allow" in _resolved("sandbox", SandRt()).description


# --------------------------------------------------------------------------
# Enablement / hiding gates
# --------------------------------------------------------------------------


def test_tag_is_ant_only():
    assert _resolved("tag", RecordingRuntime(user_type="ant")) is not None
    assert _resolved("tag", RecordingRuntime()) is None


def test_session_gated_on_remote_mode():
    assert _resolved("session", RecordingRuntime(remote_mode=True)) is not None
    assert _resolved("session", RecordingRuntime()) is None


def test_desktop_gated_on_platform():
    assert (
        _resolved(
            "desktop",
            RecordingRuntime(platform="darwin", claude_ai=True),
        )
        is not None
    )
    assert (
        _resolved(
            "desktop",
            RecordingRuntime(platform="linux", claude_ai=True),
        )
        is None
    )


def test_fast_gated_on_fast_mode():
    assert (
        _resolved(
            "fast",
            RecordingRuntime(fast_mode=True, claude_ai=True),
        )
        is not None
    )
    assert _resolved("fast", RecordingRuntime(claude_ai=True)) is None


def test_chrome_gated_on_interactive():
    assert (
        _resolved(
            "chrome",
            RecordingRuntime(non_interactive=False, claude_ai=True),
        )
        is not None
    )
    assert (
        _resolved(
            "chrome",
            RecordingRuntime(non_interactive=True, claude_ai=True),
        )
        is None
    )


def test_doctor_disabled_by_env():
    assert _resolved("doctor", RecordingRuntime()) is not None
    assert (
        _resolved(
            "doctor",
            RecordingRuntime(env={"DISABLE_DOCTOR_COMMAND": "1"}),
        )
        is None
    )


def test_ultrareview_gated():
    class UrRt(RecordingRuntime):
        def ultrareview_enabled(self):
            return True

    assert _resolved("ultrareview", UrRt()) is not None
    assert _resolved("ultrareview", RecordingRuntime()) is None


def test_upgrade_hidden_for_enterprise():
    assert (
        _resolved(
            "upgrade",
            RecordingRuntime(subscription="pro", claude_ai=True),
        )
        is not None
    )
    assert (
        _resolved(
            "upgrade",
            RecordingRuntime(subscription="enterprise", claude_ai=True),
        )
        is None
    )


def test_output_style_is_hidden():
    assert _resolved("output-style", RecordingRuntime()).hidden is True


# --------------------------------------------------------------------------
# Feature-gated commands (conditional registration in commands.ts)
# --------------------------------------------------------------------------


def test_feature_gated_dark_by_default():
    for name in ("fork", "buddy", "peers", "workflows", "torch", "brief", "ultraplan"):
        resolved = _resolved(name, RecordingRuntime())
        assert resolved is None or resolved.name != name, name


def test_fork_enabled_by_flag():
    assert (
        _resolved("fork", RecordingRuntime(features={"FORK_SUBAGENT"})).name == "fork"
    )


def test_remote_control_needs_flag_and_bridge():
    class BridgeRt(RecordingRuntime):
        def bridge_enabled(self):
            return True

    on = BridgeRt(features={"BRIDGE_MODE"})
    assert _resolved("remote-control", on) is not None
    # flag without bridge enabled → still dark
    assert (
        _resolved(
            "remote-control",
            RecordingRuntime(features={"BRIDGE_MODE"}),
        )
        is None
    )


def test_remote_control_server_needs_two_flags():
    one = RecordingRuntime(features={"DAEMON"})
    both = RecordingRuntime(features={"DAEMON", "BRIDGE_MODE"})
    assert _resolved("remote-control-server", one) is None
    assert _resolved("remote-control-server", both) is not None


def test_brief_enabled_by_either_kairos_flag():
    assert _resolved("brief", RecordingRuntime(features={"KAIROS"})) is not None
    assert _resolved("brief", RecordingRuntime(features={"KAIROS_BRIEF"})) is not None


async def test_feature_gated_still_dispatch_headlessly():
    rt = RecordingRuntime(features={"FORK_SUBAGENT"})
    invocation = await build_registry().invoke("/fork do it", rt)
    assert invocation is not None
    out = invocation.result
    assert out.dialog == "fork"

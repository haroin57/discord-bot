"""PoC: local-jsx (interactive) commands + feature-gated commands.

The TUI is headless here, so each command's ``call`` must record which dialog it
would open and return a :class:`JsxOutcome`. Also verifies dynamic descriptions,
per-runtime enablement/hiding gates, and that feature-gated commands stay dark
until their flag is set — matching the conditional registration in ``commands.ts``.
"""

from __future__ import annotations

from free_code_commands.commands import feature_gated as fg
from free_code_commands.commands import jsx_commands as jc
from free_code_commands.runtime import RecordingRuntime
from free_code_commands.types import JsxOutcome


def _jsx(name):
    return next(c for c in jc.COMMANDS if c.name == name)


def _gated(name):
    return next(c for c in fg.COMMANDS if c.name == name)


# --------------------------------------------------------------------------
# Headless dialog contract
# --------------------------------------------------------------------------


async def test_every_jsx_command_opens_its_dialog():
    for cmd in jc.COMMANDS:
        rt = RecordingRuntime()
        out = await cmd.call("some args", rt)
        assert isinstance(out, JsxOutcome)
        assert out.dialog == cmd.name
        assert rt.opened_dialogs and rt.opened_dialogs[0][0] == cmd.name
        # args are threaded into the recorded dialog payload
        assert rt.opened_dialogs[0][1].get("args") == "some args"


# --------------------------------------------------------------------------
# Dynamic descriptions (get description() in the TS)
# --------------------------------------------------------------------------


def test_model_description_shows_current_model():
    desc = _jsx("model").resolved_description(RecordingRuntime(main_loop_model="opus-4.8"))
    assert "currently opus-4.8" in desc


def test_fast_description_shows_fast_model():
    class FastRt(RecordingRuntime):
        def fast_mode_model_display(self):
            return "Haiku"

    assert "Haiku only" in _jsx("fast").resolved_description(FastRt())


def test_login_description_switches_on_auth():
    signin = _jsx("login").resolved_description(RecordingRuntime())
    switch = _jsx("login").resolved_description(RecordingRuntime(api_key_auth=True))
    assert signin == "Sign in with your Anthropic account"
    assert switch == "Switch Anthropic accounts"


def test_terminal_setup_description_and_hiding():
    ts = _jsx("terminal-setup")
    apple = ts.resolved_description(RecordingRuntime(terminal="Apple_Terminal"))
    other = ts.resolved_description(RecordingRuntime(terminal="xterm"))
    assert "Option+Enter" in apple
    assert "Shift+Enter" in other
    # hidden on native CSI-u terminals
    assert ts.hidden(RecordingRuntime(terminal="kitty")) is True
    assert ts.hidden(RecordingRuntime(terminal="xterm")) is False


def test_sandbox_description_is_dynamic():
    class SandRt(RecordingRuntime):
        def sandbox_status(self):
            return "sandbox enabled (auto-allow)"

    assert "auto-allow" in _jsx("sandbox").resolved_description(SandRt())


# --------------------------------------------------------------------------
# Enablement / hiding gates
# --------------------------------------------------------------------------


def test_tag_is_ant_only():
    assert _jsx("tag").enabled(RecordingRuntime(user_type="ant")) is True
    assert _jsx("tag").enabled(RecordingRuntime()) is False


def test_session_gated_on_remote_mode():
    assert _jsx("session").enabled(RecordingRuntime(remote_mode=True)) is True
    assert _jsx("session").enabled(RecordingRuntime()) is False
    assert _jsx("session").hidden(RecordingRuntime()) is True


def test_desktop_gated_on_platform():
    assert _jsx("desktop").enabled(RecordingRuntime(platform="darwin")) is True
    assert _jsx("desktop").enabled(RecordingRuntime(platform="linux")) is False


def test_fast_gated_on_fast_mode():
    assert _jsx("fast").enabled(RecordingRuntime(fast_mode=True)) is True
    assert _jsx("fast").enabled(RecordingRuntime()) is False


def test_chrome_gated_on_interactive():
    assert _jsx("chrome").enabled(RecordingRuntime(non_interactive=False)) is True
    assert _jsx("chrome").enabled(RecordingRuntime(non_interactive=True)) is False


def test_doctor_disabled_by_env():
    assert _jsx("doctor").enabled(RecordingRuntime()) is True
    assert _jsx("doctor").enabled(RecordingRuntime(env={"DISABLE_DOCTOR_COMMAND": "1"})) is False


def test_ultrareview_gated():
    class UrRt(RecordingRuntime):
        def ultrareview_enabled(self):
            return True

    assert _jsx("ultrareview").enabled(UrRt()) is True
    assert _jsx("ultrareview").enabled(RecordingRuntime()) is False


def test_upgrade_hidden_for_enterprise():
    assert _jsx("upgrade").enabled(RecordingRuntime(subscription="pro")) is True
    assert _jsx("upgrade").enabled(RecordingRuntime(subscription="enterprise")) is False


def test_output_style_is_hidden():
    assert _jsx("output-style").hidden(RecordingRuntime()) is True


# --------------------------------------------------------------------------
# Feature-gated commands (conditional registration in commands.ts)
# --------------------------------------------------------------------------


def test_feature_gated_dark_by_default():
    for name in ("fork", "buddy", "peers", "workflows", "torch", "brief", "ultraplan"):
        assert _gated(name).enabled(RecordingRuntime()) is False, name


def test_fork_enabled_by_flag():
    assert _gated("fork").enabled(RecordingRuntime(features={"FORK_SUBAGENT"})) is True


def test_remote_control_needs_flag_and_bridge():
    class BridgeRt(RecordingRuntime):
        def bridge_enabled(self):
            return True

    on = BridgeRt(features={"BRIDGE_MODE"})
    assert _gated("remote-control").enabled(on) is True
    # flag without bridge enabled → still dark
    assert _gated("remote-control").enabled(RecordingRuntime(features={"BRIDGE_MODE"})) is False


def test_remote_control_server_needs_two_flags():
    one = RecordingRuntime(features={"DAEMON"})
    both = RecordingRuntime(features={"DAEMON", "BRIDGE_MODE"})
    assert _gated("remote-control-server").enabled(one) is False
    assert _gated("remote-control-server").enabled(both) is True


def test_brief_enabled_by_either_kairos_flag():
    assert _gated("brief").enabled(RecordingRuntime(features={"KAIROS"})) is True
    assert _gated("brief").enabled(RecordingRuntime(features={"KAIROS_BRIEF"})) is True


async def test_feature_gated_still_dispatch_headlessly():
    rt = RecordingRuntime(features={"FORK_SUBAGENT"})
    out = await _gated("fork").call("do it", rt)
    assert out.dialog == "fork"

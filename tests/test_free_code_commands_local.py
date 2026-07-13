"""PoC: local-type commands — headless computation + branching parity.

Each ``type: 'local'`` command's ``call`` is exercised through a
:class:`RecordingRuntime`, asserting the ported control flow matches the upstream
(subscriber vs. not, ant-only branches, toggles, delegated side effects).
"""

from __future__ import annotations

import pytest

from free_code_commands.commands import local_commands as lc
from free_code_commands.runtime import RecordingRuntime


def _by_name(name):
    return next(c for c in lc.COMMANDS if c.name == name)


# --------------------------------------------------------------------------
# /cost
# --------------------------------------------------------------------------


async def test_cost_non_subscriber_shows_total():
    rt = RecordingRuntime(total_cost="Total cost: $9.99")
    out = await _by_name("cost").call("", rt)
    assert out.value == "Total cost: $9.99"


async def test_cost_subscriber_message():
    out = await _by_name("cost").call("", RecordingRuntime(claude_ai=True))
    assert "using your subscription" in out.value


async def test_cost_subscriber_overage_message():
    out = await _by_name("cost").call("", RecordingRuntime(claude_ai=True, using_overage=True))
    assert "using your overages" in out.value


async def test_cost_ant_subscriber_appends_breakdown():
    rt = RecordingRuntime(claude_ai=True, user_type="ant", total_cost="Total cost: $1.00")
    out = await _by_name("cost").call("", rt)
    assert "[ANT-ONLY]" in out.value
    assert "Total cost: $1.00" in out.value


def test_cost_hidden_for_subscriber_but_not_ant():
    cost = _by_name("cost")
    assert cost.hidden(RecordingRuntime(claude_ai=True)) is True
    assert cost.hidden(RecordingRuntime(claude_ai=True, user_type="ant")) is False
    assert cost.hidden(RecordingRuntime()) is False


# --------------------------------------------------------------------------
# /version
# --------------------------------------------------------------------------


async def test_version_plain_and_with_build_time():
    assert (await _by_name("version").call("", RecordingRuntime(version="2.0.0"))).value == "2.0.0"
    out = await _by_name("version").call("", RecordingRuntime(version="2.0.0", build_time="2026-01-01"))
    assert out.value == "2.0.0 (built 2026-01-01)"


def test_version_is_ant_only():
    v = _by_name("version")
    assert v.enabled(RecordingRuntime(user_type="ant")) is True
    assert v.enabled(RecordingRuntime()) is False


# --------------------------------------------------------------------------
# /clear, /compact, /context
# --------------------------------------------------------------------------


async def test_clear_starts_new_conversation():
    rt = RecordingRuntime()
    out = await _by_name("clear").call("", rt)
    assert rt.cleared is True
    assert out.value == ""


async def test_compact_requires_messages():
    with pytest.raises(ValueError):
        await _by_name("compact").call("", RecordingRuntime(messages=[]))


async def test_compact_returns_compact_result():
    rt = RecordingRuntime(messages=[{"role": "user"}])
    out = await _by_name("compact").call("keep the API notes", rt)
    assert out.type == "compact"


def test_compact_disabled_by_env():
    compact = _by_name("compact")
    assert compact.enabled(RecordingRuntime()) is True
    assert compact.enabled(RecordingRuntime(env={"DISABLE_COMPACT": "1"})) is False


async def test_context_noninteractive_reports_usage():
    rt = RecordingRuntime(non_interactive=True, context_report="Context: 42% used")
    out = await _by_name("context").call("", rt)
    assert out.value == "Context: 42% used"


# --------------------------------------------------------------------------
# /vim toggle
# --------------------------------------------------------------------------


async def test_vim_toggles_mode():
    rt = RecordingRuntime(vim_mode=False)
    out1 = await _by_name("vim").call("", rt)
    assert "set to vim" in out1.value and rt.get_vim_mode() is True
    out2 = await _by_name("vim").call("", rt)
    assert "set to normal" in out2.value and rt.get_vim_mode() is False


# --------------------------------------------------------------------------
# /files, /heapdump, /release-notes, /reload-plugins, /rewind, /stickers
# --------------------------------------------------------------------------


async def test_files_lists_context_or_empty():
    assert "No files" in (await _by_name("files").call("", RecordingRuntime())).value
    rt = RecordingRuntime(context_files=["a.py", "b.py"])
    out = await _by_name("files").call("", rt)
    assert "a.py" in out.value and "b.py" in out.value


async def test_heapdump_reports_paths():
    out = await _by_name("heapdump").call("", RecordingRuntime())
    assert ".heapsnapshot" in out.value


async def test_release_notes_fallback_to_changelog():
    out = await _by_name("release-notes").call("", RecordingRuntime())
    assert "changelog" in out.value.lower()


async def test_release_notes_formats_groups():
    rt = RecordingRuntime(release_note_groups=[("1.2.0", ["Added X", "Fixed Y"])])
    out = await _by_name("release-notes").call("", rt)
    assert "Version 1.2.0:" in out.value and "· Added X" in out.value


async def test_reload_plugins_summary():
    rt = RecordingRuntime(plugin_refresh={
        "enabled_count": 2, "command_count": 3, "agent_count": 0,
        "hook_count": 1, "mcp_count": 0, "lsp_count": 0, "error_count": 0,
    })
    out = await _by_name("reload-plugins").call("", rt)
    assert "Reloaded:" in out.value
    assert "2 plugins" in out.value and "3 skills" in out.value


async def test_rewind_opens_selector_and_skips():
    rt = RecordingRuntime()
    out = await _by_name("rewind").call("", rt)
    assert out.type == "skip"
    assert any(name == "rewind:message-selector" for name, _ in rt.opened_dialogs)


async def test_stickers_opens_browser():
    rt = RecordingRuntime(browser_success=True)
    out = await _by_name("stickers").call("", rt)
    assert "stickermule.com/claudecode" in rt.opened_urls[0]
    assert "Opening" in out.value


# --------------------------------------------------------------------------
# /voice, /keybindings, /advisor gating + logic
# --------------------------------------------------------------------------


async def test_voice_requires_auth_when_disabled():
    out = await _by_name("voice").call("", RecordingRuntime(voice_mode_enabled=False))
    assert "/login" in out.value or "not available" in out.value


async def test_keybindings_disabled_message():
    out = await _by_name("keybindings").call("", RecordingRuntime())
    assert "not enabled" in out.value


async def test_advisor_not_set_message():
    rt = RecordingRuntime(can_configure_advisor=True, advisor_model=None)
    out = await _by_name("advisor").call("", rt)
    assert "Advisor: not set" in out.value


async def test_advisor_set_and_unset():
    rt = RecordingRuntime(can_configure_advisor=True, main_loop_model="opus")
    out = await _by_name("advisor").call("opus", rt)
    assert "Advisor set to opus" in out.value
    assert rt.advisor_model() == "opus"
    out2 = await _by_name("advisor").call("unset", rt)
    assert "disabled" in out2.value.lower()


def test_advisor_hidden_without_capability():
    advisor = _by_name("advisor")
    assert advisor.hidden(RecordingRuntime()) is True
    assert advisor.hidden(RecordingRuntime(can_configure_advisor=True)) is False


# --------------------------------------------------------------------------
# /bridge-kick (ant-only fault injection dispatch)
# --------------------------------------------------------------------------


async def test_bridge_kick_needs_debug_handle():
    out = await _by_name("bridge-kick").call("status", RecordingRuntime(user_type="ant"))
    assert "No bridge debug handle" in out.value


async def test_bridge_kick_close_dispatch():
    class WithBridge(RecordingRuntime):
        def bridge_debug_available(self):
            return True

    out = await _by_name("bridge-kick").call("close 1002", WithBridge(user_type="ant"))
    assert "close(1002)" in out.value


def test_bridge_kick_is_ant_only():
    bk = _by_name("bridge-kick")
    assert bk.enabled(RecordingRuntime(user_type="ant")) is True
    assert bk.enabled(RecordingRuntime()) is False


# --------------------------------------------------------------------------
# /extra-usage (non-interactive twin) gating
# --------------------------------------------------------------------------


def test_extra_usage_noninteractive_gating():
    cmd = _by_name("extra-usage")
    on = RecordingRuntime(overage_allowed=True, non_interactive=True)
    off = RecordingRuntime(overage_allowed=True, non_interactive=False)
    assert cmd.enabled(on) is True
    assert cmd.enabled(off) is False  # interactive twin handles that case

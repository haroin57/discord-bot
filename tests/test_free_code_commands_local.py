"""PoC: local-type commands — headless computation + branching parity.

Each ``type: 'local'`` command is exercised through the registry invocation
:class:`RecordingRuntime`, asserting the ported control flow matches the upstream
(subscriber vs. not, ant-only branches, toggles, delegated side effects).
"""

from __future__ import annotations

import pytest

from free_code_commands import build_registry
from free_code_commands.runtime import RecordingRuntime
from free_code_commands.types import CommandType


async def _invoke(name, args, runtime):
    text = f"/{name} {args}".rstrip()
    invocation = await build_registry().invoke(text, runtime)
    assert invocation is not None
    return invocation.result


# --------------------------------------------------------------------------
# /cost
# --------------------------------------------------------------------------


async def test_cost_non_subscriber_shows_total():
    rt = RecordingRuntime(total_cost="Total cost: $9.99")
    out = await _invoke("cost", "", rt)
    assert out.value == "Total cost: $9.99"


async def test_cost_subscriber_message():
    out = await _invoke("cost", "", RecordingRuntime(claude_ai=True))
    assert "using your subscription" in out.value


async def test_cost_subscriber_overage_message():
    out = await _invoke(
        "cost",
        "",
        RecordingRuntime(claude_ai=True, using_overage=True),
    )
    assert "using your overages" in out.value


async def test_cost_ant_subscriber_appends_breakdown():
    rt = RecordingRuntime(
        claude_ai=True, user_type="ant", total_cost="Total cost: $1.00"
    )
    out = await _invoke("cost", "", rt)
    assert "[ANT-ONLY]" in out.value
    assert "Total cost: $1.00" in out.value


def test_cost_hidden_for_subscriber_but_not_ant():
    registry = build_registry()
    assert registry.resolve("cost", RecordingRuntime(claude_ai=True)).hidden is True
    assert (
        registry.resolve(
            "cost",
            RecordingRuntime(claude_ai=True, user_type="ant"),
        ).hidden
        is False
    )
    assert registry.resolve("cost", RecordingRuntime()).hidden is False


# --------------------------------------------------------------------------
# /version
# --------------------------------------------------------------------------


async def test_version_plain_and_with_build_time():
    runtime = RecordingRuntime(version="2.0.0", user_type="ant")
    assert (await _invoke("version", "", runtime)).value == "2.0.0"
    out = await _invoke(
        "version",
        "",
        RecordingRuntime(
            version="2.0.0",
            build_time="2026-01-01",
            user_type="ant",
        ),
    )
    assert out.value == "2.0.0 (built 2026-01-01)"


def test_version_is_ant_only():
    registry = build_registry()
    assert registry.resolve("version", RecordingRuntime(user_type="ant")) is not None
    assert registry.resolve("version", RecordingRuntime()) is None


# --------------------------------------------------------------------------
# /clear, /compact, /context
# --------------------------------------------------------------------------


async def test_clear_starts_new_conversation():
    rt = RecordingRuntime()
    out = await _invoke("clear", "", rt)
    assert rt.cleared is True
    assert out.value == ""


async def test_compact_requires_messages():
    with pytest.raises(ValueError):
        await _invoke("compact", "", RecordingRuntime(messages=[]))


async def test_compact_returns_compact_result():
    rt = RecordingRuntime(messages=[{"role": "user"}])
    out = await _invoke("compact", "keep the API notes", rt)
    assert out.type == "compact"


def test_compact_disabled_by_env():
    registry = build_registry()
    assert registry.resolve("compact", RecordingRuntime()) is not None
    assert (
        registry.resolve(
            "compact",
            RecordingRuntime(env={"DISABLE_COMPACT": "1"}),
        )
        is None
    )


async def test_context_noninteractive_reports_usage():
    rt = RecordingRuntime(non_interactive=True, context_report="Context: 42% used")
    out = await _invoke("context", "", rt)
    assert out.value == "Context: 42% used"


# --------------------------------------------------------------------------
# /vim toggle
# --------------------------------------------------------------------------


async def test_vim_toggles_mode():
    rt = RecordingRuntime(vim_mode=False)
    out1 = await _invoke("vim", "", rt)
    assert "set to vim" in out1.value and rt.get_vim_mode() is True
    out2 = await _invoke("vim", "", rt)
    assert "set to normal" in out2.value and rt.get_vim_mode() is False


# --------------------------------------------------------------------------
# /files, /heapdump, /release-notes, /reload-plugins, /rewind, /stickers
# --------------------------------------------------------------------------


async def test_files_lists_context_or_empty():
    assert (
        "No files"
        in (await _invoke("files", "", RecordingRuntime(user_type="ant"))).value
    )
    rt = RecordingRuntime(context_files=["a.py", "b.py"], user_type="ant")
    out = await _invoke("files", "", rt)
    assert "a.py" in out.value and "b.py" in out.value


async def test_heapdump_reports_paths():
    out = await _invoke("heapdump", "", RecordingRuntime(user_type="ant"))
    assert ".heapsnapshot" in out.value


async def test_release_notes_fallback_to_changelog():
    out = await _invoke("release-notes", "", RecordingRuntime())
    assert "changelog" in out.value.lower()


async def test_release_notes_formats_groups():
    rt = RecordingRuntime(release_note_groups=[("1.2.0", ["Added X", "Fixed Y"])])
    out = await _invoke("release-notes", "", rt)
    assert "Version 1.2.0:" in out.value and "· Added X" in out.value


async def test_reload_plugins_summary():
    rt = RecordingRuntime(
        plugin_refresh={
            "enabled_count": 2,
            "command_count": 3,
            "agent_count": 0,
            "hook_count": 1,
            "mcp_count": 0,
            "lsp_count": 0,
            "error_count": 0,
        }
    )
    out = await _invoke("reload-plugins", "", rt)
    assert "Reloaded:" in out.value
    assert "2 plugins" in out.value and "3 skills" in out.value


async def test_rewind_opens_selector_and_skips():
    rt = RecordingRuntime()
    out = await _invoke("rewind", "", rt)
    assert out.type == "skip"
    assert any(name == "rewind:message-selector" for name, _ in rt.opened_dialogs)


async def test_stickers_opens_browser():
    rt = RecordingRuntime(browser_success=True)
    out = await _invoke("stickers", "", rt)
    assert "stickermule.com/claudecode" in rt.opened_urls[0]
    assert "Opening" in out.value


# --------------------------------------------------------------------------
# /voice, /keybindings, /advisor gating + logic
# --------------------------------------------------------------------------


async def test_voice_cannot_invoke_when_disabled():
    invocation = await build_registry().invoke(
        "/voice",
        RecordingRuntime(voice_mode_enabled=False),
    )
    assert invocation is None


async def test_keybindings_cannot_invoke_when_disabled():
    invocation = await build_registry().invoke(
        "/keybindings",
        RecordingRuntime(),
    )
    assert invocation is None


async def test_advisor_not_set_message():
    rt = RecordingRuntime(can_configure_advisor=True, advisor_model=None)
    out = await _invoke("advisor", "", rt)
    assert "Advisor: not set" in out.value


async def test_advisor_set_and_unset():
    rt = RecordingRuntime(can_configure_advisor=True, main_loop_model="opus")
    out = await _invoke("advisor", "opus", rt)
    assert "Advisor set to opus" in out.value
    assert rt.advisor_model() == "opus"
    out2 = await _invoke("advisor", "unset", rt)
    assert "disabled" in out2.value.lower()


def test_advisor_catalog_is_gated_by_capability():
    registry = build_registry()
    assert registry.resolve("advisor", RecordingRuntime()) is None
    assert (
        registry.resolve(
            "advisor",
            RecordingRuntime(can_configure_advisor=True),
        ).hidden
        is False
    )


# --------------------------------------------------------------------------
# /bridge-kick (ant-only fault injection dispatch)
# --------------------------------------------------------------------------


async def test_bridge_kick_needs_debug_handle():
    out = await _invoke(
        "bridge-kick",
        "status",
        RecordingRuntime(user_type="ant"),
    )
    assert "No bridge debug handle" in out.value


async def test_bridge_kick_close_dispatch():
    class WithBridge(RecordingRuntime):
        def bridge_debug_available(self):
            return True

    out = await _invoke(
        "bridge-kick",
        "close 1002",
        WithBridge(user_type="ant"),
    )
    assert "close(1002)" in out.value


def test_bridge_kick_is_ant_only():
    registry = build_registry()
    assert (
        registry.resolve("bridge-kick", RecordingRuntime(user_type="ant")) is not None
    )
    assert registry.resolve("bridge-kick", RecordingRuntime()) is None


# --------------------------------------------------------------------------
# /extra-usage (non-interactive twin) gating
# --------------------------------------------------------------------------


def test_extra_usage_noninteractive_gating():
    registry = build_registry()
    on = RecordingRuntime(overage_allowed=True, non_interactive=True)
    off = RecordingRuntime(overage_allowed=True, non_interactive=False)
    assert registry.resolve("extra-usage", on).type is CommandType.LOCAL
    assert registry.resolve("extra-usage", off).type is CommandType.LOCAL_JSX

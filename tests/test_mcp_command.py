"""Argument dispatch for the ported free-code /mcp command."""

from __future__ import annotations

from free_code_commands.commands import jsx_commands
from free_code_commands.runtime import RecordingRuntime


def _command():
    return next(command for command in jsx_commands.COMMANDS if command.name == "mcp")


def _runtime():
    rows = [
        {
            "key": "alpha",
            "transport": "stdio",
            "tier": "web",
            "enabled": True,
            "selected": True,
            "probe_status": "unprobed",
            "tool_count": 0,
        },
        {
            "key": "beta",
            "transport": "http",
            "tier": "docs",
            "enabled": True,
            "selected": False,
            "probe_status": "unprobed",
            "tool_count": 0,
        },
    ]
    probes = {
        "alpha": {
            "key": "alpha",
            "status": "reachable",
            "tool_names": ("a", "b"),
            "checked_at": "test",
            "error": None,
        },
    }
    return RecordingRuntime(mcp_servers=rows, mcp_probe_results=probes)


async def test_empty_args_returns_structured_status():
    outcome = await _command().call("", _runtime())
    assert outcome.dialog == "mcp"
    assert outcome.data["action"] == "status"
    assert len(outcome.data["servers"]) == 2
    assert "connected" not in (outcome.message or "")


async def test_enable_disable_and_idempotence():
    runtime = _runtime()
    disabled = await _command().call("disable alpha", runtime)
    assert disabled.data["mutation"]["changed_keys"] == ("alpha",)
    again = await _command().call("disable alpha", runtime)
    assert "already disabled" in (again.message or "")
    enabled = await _command().call("enable alpha", runtime)
    assert enabled.data["mutation"]["changed_keys"] == ("alpha",)


async def test_all_and_missing_target():
    runtime = _runtime()
    outcome = await _command().call("disable all", runtime)
    assert outcome.data["mutation"]["changed_keys"] == ("alpha", "beta")
    missing = await _command().call("enable", runtime)
    assert "Usage:" in (missing.message or "")


async def test_unknown_server_and_action():
    runtime = _runtime()
    missing = await _command().call("reconnect missing", runtime)
    assert "Unknown MCP server" in (missing.message or "")
    unknown = await _command().call("oauth alpha", runtime)
    assert "Unknown MCP action" in (unknown.message or "")


async def test_reconnect_is_temporary_probe_and_tools_action():
    runtime = _runtime()
    probe = await _command().call("reconnect alpha", runtime)
    assert probe.data["probe"]["status"] == "reachable"
    assert "2 tools" in (probe.message or "")
    tools = await _command().call("tools alpha", runtime)
    assert tools.data["tools"] == ["a", "b"]

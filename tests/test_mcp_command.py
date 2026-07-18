"""Argument dispatch for the ported free-code /mcp command."""

from __future__ import annotations

from free_code_commands import build_registry
from free_code_commands.runtime import RecordingRuntime


async def _invoke(args, runtime):
    text = f"/mcp {args}".rstrip()
    invocation = await build_registry().invoke(text, runtime)
    assert invocation is not None
    return invocation.result


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
    outcome = await _invoke("", _runtime())
    assert outcome.dialog == "mcp"
    assert outcome.data["action"] == "status"
    assert len(outcome.data["servers"]) == 2
    assert "connected" not in (outcome.message or "")


async def test_enable_disable_and_idempotence():
    runtime = _runtime()
    disabled = await _invoke("disable alpha", runtime)
    assert disabled.data["mutation"]["changed_keys"] == ("alpha",)
    again = await _invoke("disable alpha", runtime)
    assert "already disabled" in (again.message or "")
    enabled = await _invoke("enable alpha", runtime)
    assert enabled.data["mutation"]["changed_keys"] == ("alpha",)


async def test_all_and_missing_target():
    runtime = _runtime()
    outcome = await _invoke("disable all", runtime)
    assert outcome.data["mutation"]["changed_keys"] == ("alpha", "beta")
    missing = await _invoke("enable", runtime)
    assert "Usage:" in (missing.message or "")


async def test_unknown_server_and_action():
    runtime = _runtime()
    missing = await _invoke("reconnect missing", runtime)
    assert "Unknown MCP server" in (missing.message or "")
    unknown = await _invoke("oauth alpha", runtime)
    assert "Unknown MCP action" in (unknown.message or "")


async def test_reconnect_is_temporary_probe_and_tools_action():
    runtime = _runtime()
    probe = await _invoke("reconnect alpha", runtime)
    assert probe.data["probe"]["status"] == "reachable"
    assert "2 tools" in (probe.message or "")
    tools = await _invoke("tools alpha", runtime)
    assert tools.data["tools"] == ["a", "b"]

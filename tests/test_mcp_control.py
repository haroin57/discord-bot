"""Persistent MCP control, probe lifecycle, and disabled-path contracts."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest

import services.mcp_control as control_mod
from services.mcp_control import McpControlService


def _servers():
    return {
        "alpha": {
            "command": "secret-command",
            "env": {"TOKEN": "env-secret"},
        },
        "beta": {
            "type": "http",
            "url": "https://user:password@example.invalid/mcp",
            "headers": {"Authorization": "Bearer header-secret"},
        },
    }


def test_enable_disable_all_idempotence_and_reload(tmp_path):
    path = tmp_path / "mcp.json"
    service = McpControlService(_servers(), state_path=path)

    first = service.set_enabled("alpha", False)
    assert first.changed_keys == ("alpha",)
    assert service.set_enabled("alpha", False).changed is False
    assert service.is_enabled("alpha") is False

    reloaded = McpControlService(_servers(), state_path=path)
    assert reloaded.disabled_keys() == frozenset({"alpha"})
    assert reloaded.set_enabled("all", False).changed_keys == ("beta",)
    assert reloaded.disabled_keys() == frozenset({"alpha", "beta"})
    assert reloaded.set_enabled("all", True).changed_keys == ("alpha", "beta")
    assert reloaded.disabled_keys() == frozenset()


def test_unknown_server_does_not_write_state(tmp_path):
    path = tmp_path / "mcp.json"
    service = McpControlService(_servers(), state_path=path)
    with pytest.raises(KeyError):
        service.set_enabled("missing", False)
    assert not path.exists()


def test_status_payload_does_not_expose_config_secrets(tmp_path):
    service = McpControlService(_servers(), state_path=tmp_path / "mcp.json")
    rendered = repr(service.list_servers())
    for secret in ("secret-command", "env-secret", "password", "header-secret"):
        assert secret not in rendered


async def test_probe_lists_tools_and_closes_exit_stack(tmp_path, monkeypatch):
    closed = []

    async def fake_spawn(stack, key, cfg):
        async def cleanup():
            closed.append(key)

        stack.push_async_callback(cleanup)
        return SimpleNamespace(
            tools=[SimpleNamespace(name="tool_b"), SimpleNamespace(name="tool_a")]
        )

    monkeypatch.setattr(control_mod, "_spawn_for_probe", fake_spawn)
    service = McpControlService(_servers(), state_path=tmp_path / "mcp.json")
    result = await service.probe("alpha")

    assert result.status == "reachable"
    assert result.tool_names == ("tool_a", "tool_b")
    assert closed == ["alpha"]


async def test_probe_timeout_still_closes_exit_stack(tmp_path, monkeypatch):
    closed = []

    async def slow_spawn(stack, key, cfg):
        async def cleanup():
            closed.append(key)

        stack.push_async_callback(cleanup)
        await asyncio.sleep(60)

    monkeypatch.setattr(control_mod, "_spawn_for_probe", slow_spawn)
    service = McpControlService(
        _servers(), state_path=tmp_path / "mcp.json", probe_timeout=0.01,
    )
    result = await service.probe("alpha")

    assert result.status == "failed"
    assert result.error == "timeout"
    assert closed == ["alpha"]


async def test_probe_failure_redacts_exception_text(tmp_path, monkeypatch):
    secret = "Bearer private-token"

    async def fail(stack, key, cfg):
        raise RuntimeError(f"remote rejected {secret}: {cfg['env']['TOKEN']}")

    monkeypatch.setattr(control_mod, "_spawn_for_probe", fail)
    service = McpControlService(_servers(), state_path=tmp_path / "mcp.json")
    result = await service.probe("alpha")

    assert result.status == "failed"
    assert result.error == "probe failed (RuntimeError)"
    assert secret not in repr(result)
    assert "env-secret" not in repr(result)


def test_disabled_server_filtered_from_restore_static_deferred_and_pool(
    tmp_path, monkeypatch,
):
    from services import mcp_selector
    from services.claude_api import entry, mcp_client, tool_catalog
    from services.session import SessionInfo

    service = McpControlService(
        {"mem0": {"command": "/bin/true"}},
        state_path=tmp_path / "mcp.json",
    )
    service.set_enabled("mem0", False)
    monkeypatch.setattr(control_mod, "_default_service", service)

    assert mcp_selector.get_mcp_servers_by_keys(["mem0"]) == {}
    assert mcp_client.static_mcp_tools_to_anthropic({"mem0"}) == []
    assert "mem0" not in tool_catalog.deferred_catalog_keys(None)

    info = SessionInfo(session_id="s", mcp_server_keys=["mem0"])
    monkeypatch.setattr(entry, "get_session", lambda channel_id, user_id: info)
    monkeypatch.setattr(entry, "_is_session_fresh", lambda channel_id, user_id: True)
    cfg, resumable = entry._resolve_session({}, 1, set(), None)
    assert resumable is True
    assert cfg == {}
    assert info.mcp_server_keys == ["mem0"]

    async def check_pool():
        async with AsyncExitStack() as stack:
            pool = mcp_client.McpPool(
                stack=stack,
                available_configs={"mem0": {"command": "/bin/true"}},
            )
            assert pool.available_configs == {}
            with pytest.raises(KeyError):
                await pool.ensure_spawned("mem0")

    asyncio.run(check_pool())

"""Discord adapter contracts for /mcp."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_commands.mcp as mcp_command
import services.mcp_control as control_mod
from free_code_commands.runtime import RecordingRuntime
from services.mcp_control import McpControlService


class _FakeTree:
    def __init__(self):
        self.cmds = {}

    def command(self, *, name, description=None, **kwargs):
        def decorator(fn):
            self.cmds[name] = fn
            return fn

        return decorator


def _interaction(uid=123, channel_id=456):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, name="user", display_name="user"),
        channel_id=channel_id,
        response=SimpleNamespace(
            send_message=AsyncMock(),
            defer=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _register(is_admin=True):
    tree = _FakeTree()
    deps = SimpleNamespace(is_admin=lambda user: is_admin)
    mcp_command.register(tree, deps)
    return tree.cmds["mcp"]


@pytest.fixture
def service(tmp_path, monkeypatch):
    servers = {
        "alpha": {"command": "/bin/true"},
        "beta": {"type": "http", "url": "https://example.invalid/mcp"},
    }
    value = McpControlService(servers, state_path=tmp_path / "mcp.json")
    monkeypatch.setattr(control_mod, "_default_service", value)
    return value


async def test_command_registered_and_non_admin_blocked(service):
    handler = _register(is_admin=False)
    interaction = _interaction()
    await handler(interaction)
    interaction.response.send_message.assert_awaited_once_with(
        "admin only", ephemeral=True,
    )


async def test_status_reply_is_ephemeral_and_has_owner_view(service):
    handler = _register()
    interaction = _interaction()
    await handler(interaction)
    call = interaction.response.send_message.await_args
    assert call.kwargs["ephemeral"] is True
    view = call.kwargs["view"]
    assert isinstance(view, mcp_command._McpView)
    assert view.owner_id == interaction.user.id
    assert "persistent setting" in call.args[0]
    assert "connected" not in call.args[0]


async def test_enable_disable_direct_action_uses_persistent_service(service):
    handler = _register()
    interaction = _interaction()
    await handler(interaction, "disable", "alpha")
    assert service.is_enabled("alpha") is False
    call = interaction.response.send_message.await_args
    assert call.kwargs["ephemeral"] is True


async def test_view_owner_restriction(service):
    runtime = mcp_command._DiscordMcpRuntime(456, 123)
    view = mcp_command._McpView(runtime, owner_id=123)
    own = _interaction(uid=123)
    other = _interaction(uid=999)
    assert await view.interaction_check(own) is True
    assert await view.interaction_check(other) is False
    other.response.send_message.assert_awaited_once()
    assert other.response.send_message.await_args.kwargs["ephemeral"] is True


def test_tool_list_paginates():
    runtime = RecordingRuntime(
        mcp_servers=[{
            "key": "alpha",
            "transport": "stdio",
            "tier": "web",
            "enabled": True,
            "selected": False,
            "probe_status": "reachable",
            "tool_count": 31,
        }],
        mcp_probe_results={
            "alpha": {
                "key": "alpha",
                "status": "reachable",
                "tool_names": tuple(f"tool_{i:02d}" for i in range(31)),
                "checked_at": "test",
                "error": None,
            },
        },
    )
    view = mcp_command._McpView(runtime, owner_id=123, selected_key="alpha")
    view.mode = "tools"
    view.refresh()
    assert view._tool_page_count() == 3
    assert "Tools page 1/3" in view.render()

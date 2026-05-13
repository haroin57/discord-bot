"""Tests for services.claude_api.restricted — policy gate fuzz + invariants."""

import pytest

from services.claude_api.restricted import (
    RESTRICTED_DENY_LIST,
    make_can_use_tool,
)


class TestMakeCanUseTool:
    def test_admin_default_allows_everything(self):
        gate = make_can_use_tool(restricted=False)
        for name in ("Bash", "Read", "WebSearch", "mcp__github__list_repos", "anything"):
            allowed, _ = gate(name, {})
            assert allowed is True

    @pytest.mark.parametrize("tool", RESTRICTED_DENY_LIST)
    def test_restricted_denies_every_dangerous_tool(self, tool):
        gate = make_can_use_tool(restricted=True)
        allowed, reason = gate(tool, {})
        assert allowed is False
        assert reason

    @pytest.mark.parametrize("tool", RESTRICTED_DENY_LIST)
    def test_restricted_denies_mcp_qualified_variant(self, tool):
        # If an MCP server ever exposes a tool whose bare name happens to
        # collide with our deny list, the gate must still refuse it.
        gate = make_can_use_tool(restricted=True)
        allowed, _ = gate(f"mcp__some_server__{tool}", {})
        assert allowed is False

    def test_admin_with_empty_allow_list_denies_all(self):
        gate = make_can_use_tool(restricted=False, allowed_tools=[])
        allowed, reason = gate("Bash", {})
        assert allowed is False
        assert "no tools" in reason

    def test_admin_allow_list_respected(self):
        gate = make_can_use_tool(restricted=False, allowed_tools=["Read"])
        assert gate("Read", {})[0] is True
        assert gate("Bash", {})[0] is False

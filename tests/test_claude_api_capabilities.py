from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.claude_api import agent_tool
from services.claude_api.capabilities import (
    is_mcp_tool_name,
    mcp_tools_for_dispatcher,
    require_dispatchable_advertisement,
)
from services.deep_research.types import SubAgentResult


def test_mcp_dispatcher_capabilities_filter_non_mcp_advertisement() -> None:
    tools = [
        {"name": "Read"},
        {"name": "Agent"},
        {"name": "mcp__brave-search__web"},
        {"name": "tool_search_tool_regex"},
    ]

    assert mcp_tools_for_dispatcher(tools) == [
        {"name": "mcp__brave-search__web"},
    ]
    with pytest.raises(ValueError, match="Read, Agent"):
        require_dispatchable_advertisement(tools[:2], is_mcp_tool_name)


@pytest.mark.asyncio
async def test_agent_child_advertises_only_tools_its_dispatcher_can_execute() -> None:
    captured: dict = {}

    async def fake_run_subagent(**kwargs):
        captured.update(kwargs)
        return SubAgentResult(subq="p", summary_md="ok", ok=True)

    ctx = agent_tool.AgentToolCtx(
        api_client=MagicMock(),
        pool=MagicMock(),
        tools_for_api=[
            {"name": "Read"},
            {"name": "Agent"},
            {"name": "mcp__brave-search__web"},
        ],
        channel_id=1,
        parent_model="claude-opus-4-8",
    )
    with patch(
        "services.deep_research.registry.run_subagent",
        new=fake_run_subagent,
    ):
        result = await agent_tool.execute_agent_tool(
            {"description": "test", "prompt": "p"},
            ctx,
        )

    assert result == "ok"
    assert captured["tools_for_api"] == [
        {"name": "mcp__brave-search__web"},
    ]

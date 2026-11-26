from __future__ import annotations


import pytest

from services.claude_api.capabilities import (
    is_mcp_tool_name,
    mcp_tools_for_dispatcher,
    require_dispatchable_advertisement,
)


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

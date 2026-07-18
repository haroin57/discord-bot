"""Tool advertisement contracts for dispatchers with bounded capabilities."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

ToolPredicate = Callable[[str], bool]


def is_mcp_tool_name(name: str) -> bool:
    parts = name.split("__", 2)
    return len(parts) == 3 and parts[0] == "mcp" and bool(parts[1] and parts[2])


def tools_for_dispatcher(
    advertised_tools: Iterable[dict[str, Any]],
    can_dispatch: ToolPredicate,
) -> list[dict[str, Any]]:
    """Keep only capabilities the paired dispatcher can execute."""
    return [
        tool for tool in advertised_tools if can_dispatch(str(tool.get("name") or ""))
    ]


def require_dispatchable_advertisement(
    advertised_tools: Iterable[dict[str, Any]],
    can_dispatch: ToolPredicate,
) -> None:
    unsupported = [
        str(tool.get("name") or "<unnamed>")
        for tool in advertised_tools
        if not can_dispatch(str(tool.get("name") or ""))
    ]
    if unsupported:
        raise ValueError(
            "advertised tools have no dispatcher capability: " + ", ".join(unsupported)
        )


def mcp_tools_for_dispatcher(
    advertised_tools: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    tools = tools_for_dispatcher(advertised_tools, is_mcp_tool_name)
    require_dispatchable_advertisement(tools, is_mcp_tool_name)
    return tools

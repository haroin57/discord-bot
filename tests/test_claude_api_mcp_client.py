"""Tests for services.claude_api.mcp_client — schema, dispatch, cleanup."""

from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.claude_api.mcp_client import (
    McpServerHandle,
    call_mcp_tool,
    kill_orphan_mcp_processes,
    mcp_tools_to_anthropic,
    spawn_servers,
)


def _fake_tool(name: str, schema: dict | None = None):
    """Mimic a Pydantic mcp.Tool minimally."""
    return SimpleNamespace(
        name=name,
        description=f"description of {name}",
        inputSchema=schema or {"type": "object", "properties": {"q": {"type": "string"}}},
    )


class TestMcpToolsToAnthropic:
    def test_empty(self):
        assert mcp_tools_to_anthropic({}) == []

    def test_qualified_name_format(self):
        handle = McpServerHandle(
            key="brave-search", session=MagicMock(),
            tools=[_fake_tool("brave_web_search")],
        )
        out = mcp_tools_to_anthropic({"brave-search": handle})
        assert out[0]["name"] == "mcp__brave-search__brave_web_search"
        assert out[0]["description"]
        assert out[0]["input_schema"]["type"] == "object"

    def test_tool_schema_shape(self):
        """Sanity check the mcp.Tool fields we depend on still exist.

        If this fails after an mcp SDK upgrade, the pin in requirements.txt
        needs review (Plan risk #3).
        """
        from mcp import Tool
        assert "name" in Tool.model_fields
        assert "description" in Tool.model_fields
        assert "inputSchema" in Tool.model_fields

    def test_missing_input_schema_falls_back(self):
        tool = SimpleNamespace(name="t", description="d", inputSchema=None)
        handle = McpServerHandle(key="x", session=MagicMock(), tools=[tool])
        out = mcp_tools_to_anthropic({"x": handle})
        assert out[0]["input_schema"] == {"type": "object", "properties": {}}


class TestCallMcpTool:
    @pytest.mark.asyncio
    async def test_dispatches_to_named_server(self):
        session = MagicMock()
        result = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")],
        )
        session.call_tool = AsyncMock(return_value=result)
        handles = {"foo": McpServerHandle(key="foo", session=session, tools=[])}

        out = await call_mcp_tool(handles, "mcp__foo__bar", {"q": "x"})
        session.call_tool.assert_awaited_once_with("bar", {"q": "x"})
        assert out == "hi"

    @pytest.mark.asyncio
    async def test_unknown_server_raises(self):
        with pytest.raises(KeyError):
            await call_mcp_tool({}, "mcp__missing__t", {})

    @pytest.mark.asyncio
    async def test_malformed_name_raises(self):
        with pytest.raises(ValueError):
            await call_mcp_tool({}, "not_qualified", {})

    @pytest.mark.asyncio
    async def test_image_block_stringified(self):
        session = MagicMock()
        result = SimpleNamespace(
            content=[SimpleNamespace(type="image", mimeType="image/png", data="abc")],
        )
        session.call_tool = AsyncMock(return_value=result)
        handles = {"foo": McpServerHandle(key="foo", session=session, tools=[])}

        out = await call_mcp_tool(handles, "mcp__foo__bar", {})
        assert "[image: image/png" in out

    @pytest.mark.asyncio
    async def test_empty_content_returns_placeholder(self):
        session = MagicMock()
        result = SimpleNamespace(content=[])
        session.call_tool = AsyncMock(return_value=result)
        handles = {"foo": McpServerHandle(key="foo", session=session, tools=[])}
        assert await call_mcp_tool(handles, "mcp__foo__bar", {}) == "(empty)"


class TestSpawnServers:
    @pytest.mark.asyncio
    async def test_failure_does_not_abort_other_servers(self):
        async def _bad(stack, name, cfg):
            raise RuntimeError("nope")

        # Patch _spawn_one so the first server fails but the second succeeds.
        calls = {"n": 0}

        async def _maybe_fail(stack, name, cfg):
            calls["n"] += 1
            if name == "bad":
                raise RuntimeError("boom")
            return McpServerHandle(key=name, session=MagicMock(), tools=[])

        async with AsyncExitStack() as stack:
            with patch("services.claude_api.mcp_client._spawn_one", side_effect=_maybe_fail):
                handles = await spawn_servers(
                    {"bad": {"command": "false"}, "good": {"command": "true"}},
                    stack,
                )
        assert "good" in handles
        assert "bad" not in handles
        assert calls["n"] == 2


class TestKillOrphanMcpProcesses:
    def test_returns_zero_when_no_psutil(self, monkeypatch):
        monkeypatch.setattr("services.claude_api.mcp_client.psutil", None)
        assert kill_orphan_mcp_processes() == 0

    def test_terminates_mcp_cmdline_match(self, monkeypatch):
        mcp = pytest.importorskip("psutil")  # noqa: F841
        child = MagicMock()
        child.cmdline.return_value = ["node", "@playwright/mcp", "--port"]
        child.terminate = MagicMock()
        child.kill = MagicMock()

        me = MagicMock()
        me.children.return_value = [child]

        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = me
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception
        fake_psutil.wait_procs.return_value = ([child], [])
        monkeypatch.setattr("services.claude_api.mcp_client.psutil", fake_psutil)

        assert kill_orphan_mcp_processes() == 1
        child.terminate.assert_called_once()

    def test_ignores_non_mcp_children(self, monkeypatch):
        child = MagicMock()
        child.cmdline.return_value = ["python", "unrelated.py"]
        me = MagicMock()
        me.children.return_value = [child]
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = me
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception
        monkeypatch.setattr("services.claude_api.mcp_client.psutil", fake_psutil)
        assert kill_orphan_mcp_processes() == 0

"""Tests for services.claude_api.mcp_client — schema, dispatch, cleanup."""

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.claude_api import mcp_client as mcp_client_mod
from services.claude_api.mcp_client import (
    McpPool,
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
    async def test_spawn_one_uses_sse_transport(self, monkeypatch):
        calls: dict[str, object] = {}

        class FakeSseTransport:
            async def __aenter__(self):
                calls["transport_entered"] = True
                return ("read-stream", "write-stream")

            async def __aexit__(self, exc_type, exc, tb):
                return False

        def fake_sse_client(url, **kwargs):
            calls["url"] = url
            calls["kwargs"] = kwargs
            return FakeSseTransport()

        class FakeClientSession:
            def __init__(self, read, write):
                calls["session_streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def initialize(self):
                calls["initialized"] = True

            async def list_tools(self):
                return SimpleNamespace(tools=[_fake_tool("search")])

        monkeypatch.setattr(mcp_client_mod, "_HAS_SSE_TRANSPORT", True)
        monkeypatch.setattr(mcp_client_mod, "sse_client", fake_sse_client)
        monkeypatch.setattr(mcp_client_mod, "ClientSession", FakeClientSession)

        async with AsyncExitStack() as stack:
            handle = await mcp_client_mod._spawn_one(
                stack,
                "shared",
                {
                    "type": "sse",
                    "url": "http://127.0.0.1:8020/sse",
                    "headers": {"X-Test": "1"},
                    "timeout": 10,
                    "sse_read_timeout": 20,
                },
            )

        assert handle.key == "shared"
        assert [tool.name for tool in handle.tools] == ["search"]
        assert calls["url"] == "http://127.0.0.1:8020/sse"
        assert calls["kwargs"] == {
            "headers": {"X-Test": "1"},
            "timeout": 10,
            "sse_read_timeout": 20,
        }
        assert calls["session_streams"] == ("read-stream", "write-stream")
        assert calls["initialized"] is True

    @pytest.mark.asyncio
    async def test_spawn_one_sse_requires_transport(self, monkeypatch):
        monkeypatch.setattr(mcp_client_mod, "_HAS_SSE_TRANSPORT", False)
        async with AsyncExitStack() as stack:
            with pytest.raises(RuntimeError, match="MCP SSE transport not available"):
                await mcp_client_mod._spawn_one(
                    stack,
                    "shared",
                    {"type": "sse", "url": "http://127.0.0.1:8020/sse"},
                )

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


def _make_handle(name: str, *, text: str = "ok") -> McpServerHandle:
    """Build a handle whose session.call_tool returns a single text block."""
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
    ))
    return McpServerHandle(key=name, session=session, tools=[])


class TestMcpPool:
    @pytest.mark.asyncio
    async def test_pool_lazy_spawns_on_first_call_tool(self):
        """call_tool on an unspawned-but-configured server triggers spawn."""
        spawn_calls: list[str] = []

        async def fake_spawn(stack, name, cfg):
            spawn_calls.append(name)
            return _make_handle(name, text="hi")

        async with AsyncExitStack() as stack:
            pool = McpPool(
                stack=stack,
                available_configs={"mem0": {"command": "/bin/true"}},
            )
            with patch("services.claude_api.mcp_client._spawn_one", side_effect=fake_spawn):
                out = await pool.call_tool("mcp__mem0__search", {"q": "x"})
        assert out == "hi"
        assert spawn_calls == ["mem0"]
        assert "mem0" in pool.handles

    @pytest.mark.asyncio
    async def test_pool_caches_handle_after_spawn(self):
        """Second call_tool reuses the spawned handle (no re-spawn)."""
        spawn_calls: list[str] = []

        async def fake_spawn(stack, name, cfg):
            spawn_calls.append(name)
            return _make_handle(name)

        async with AsyncExitStack() as stack:
            pool = McpPool(
                stack=stack,
                available_configs={"mem0": {"command": "/bin/true"}},
            )
            with patch("services.claude_api.mcp_client._spawn_one", side_effect=fake_spawn):
                await pool.call_tool("mcp__mem0__search", {})
                await pool.call_tool("mcp__mem0__add", {})
        assert spawn_calls == ["mem0"]  # only one spawn for two calls

    @pytest.mark.asyncio
    async def test_pool_raises_for_unconfigured_server(self):
        """A server not in available_configs raises KeyError."""
        async with AsyncExitStack() as stack:
            pool = McpPool(stack=stack, available_configs={})
            with pytest.raises(KeyError):
                await pool.call_tool("mcp__nope__t", {})

    @pytest.mark.asyncio
    async def test_pool_concurrent_call_spawns_once(self):
        """Parallel call_tool on the same server only spawns the subprocess once."""
        spawn_calls: list[str] = []
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()

        async def slow_spawn(stack, name, cfg):
            spawn_calls.append(name)
            spawn_started.set()
            # Hold inside _spawn_one so the second caller has time to enter
            # ensure_spawned and hit the lock instead of the fast path.
            await release_spawn.wait()
            return _make_handle(name)

        async with AsyncExitStack() as stack:
            pool = McpPool(
                stack=stack,
                available_configs={"mem0": {"command": "/bin/true"}},
            )
            with patch("services.claude_api.mcp_client._spawn_one", side_effect=slow_spawn):
                task_a = asyncio.create_task(pool.call_tool("mcp__mem0__search", {}))
                # Wait until the first spawn is in-flight, then fire the second
                await spawn_started.wait()
                task_b = asyncio.create_task(pool.call_tool("mcp__mem0__add", {}))
                await asyncio.sleep(0)  # let task_b reach the lock
                release_spawn.set()
                await asyncio.gather(task_a, task_b)

        assert spawn_calls == ["mem0"]  # lock prevented duplicate spawn

    @pytest.mark.asyncio
    async def test_pool_spawn_eager_failure_non_fatal(self):
        """spawn_eager logs and skips broken servers; others still spawn."""
        async def maybe_fail(stack, name, cfg):
            if name == "bad":
                raise RuntimeError("boom")
            return _make_handle(name)

        async with AsyncExitStack() as stack:
            pool = McpPool(
                stack=stack,
                available_configs={
                    "bad": {"command": "/bin/false"},
                    "good": {"command": "/bin/true"},
                },
            )
            with patch("services.claude_api.mcp_client._spawn_one", side_effect=maybe_fail):
                await pool.spawn_eager(["bad", "good"])
        assert "good" in pool.handles
        assert "bad" not in pool.handles


class TestStaticSchemaMergeGuard:
    """dump 失敗の残骸（空リスト）で正常スキーマを空確定させない。"""

    def test_empty_list_entries_are_skipped(self, tmp_path, monkeypatch):
        import json as _json
        from services.claude_api import mcp_client as mod

        payload = {
            "broken-server": [],  # dump 失敗の残骸
            "good-server": [{"name": "mcp__good-server__t",
                             "description": "d", "input_schema": {}}],
            "not-a-list": {"oops": True},
        }
        p = tmp_path / "static.json"
        p.write_text(_json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(mod, "_STATIC_SCHEMA_JSON", p)
        registry: dict = {}
        monkeypatch.setattr(mod, "STATIC_MCP_TOOL_SCHEMAS", registry)

        mod._load_static_schemas_from_disk()

        assert "good-server" in registry
        assert "broken-server" not in registry
        assert "not-a-list" not in registry

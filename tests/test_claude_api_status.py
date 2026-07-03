"""Tests for services.claude_api.status — safe callback + tool hint."""

from unittest.mock import AsyncMock

import pytest

from services.claude_api.status import _safe_status, _tool_input_hint, tool_oneline


class TestSafeStatus:
    @pytest.mark.asyncio
    async def test_none_callback_no_raise(self):
        await _safe_status(None, "hello")  # must not raise

    @pytest.mark.asyncio
    async def test_callback_invoked(self):
        cb = AsyncMock()
        await _safe_status(cb, "hi")
        cb.assert_awaited_once_with("hi")

    @pytest.mark.asyncio
    async def test_callback_exception_swallowed(self):
        async def cb(msg):
            raise RuntimeError("boom")
        await _safe_status(cb, "x")  # must not raise


class TestToolInputHint:
    def test_bash(self):
        assert _tool_input_hint("Bash", {"command": "ls"}) == "ls"

    def test_read(self):
        assert _tool_input_hint("Read", {"file_path": "/x"}) == "/x"

    def test_edit(self):
        assert _tool_input_hint("Edit", {"file_path": "/x", "old_string": "a"}) == "/x"

    def test_write(self):
        assert _tool_input_hint("Write", {"file_path": "/x"}) == "/x"

    def test_glob(self):
        assert _tool_input_hint("Glob", {"pattern": "**/*.py"}) == "**/*.py"

    def test_grep(self):
        assert _tool_input_hint("Grep", {"pattern": "foo"}) == "foo"

    def test_webfetch(self):
        assert _tool_input_hint("WebFetch", {"url": "https://x"}) == "https://x"

    def test_websearch(self):
        assert _tool_input_hint("WebSearch", {"query": "claude"}) == "claude"

    def test_brave(self):
        assert _tool_input_hint("brave_web_search", {"query": "x"}) == "x"

    def test_search_x(self):
        assert _tool_input_hint("search_x", {"query": "ai"}) == "ai"

    def test_agent(self):
        assert _tool_input_hint("Agent", {"description": "research"}) == "research"

    def test_scratchpad(self):
        assert _tool_input_hint("Scratchpad", {"section": "進捗"}) == "進捗"

    def test_scratchpad_partial_snapshot_no_section_yet(self):
        # input_json snapshots arrive incrementally — "op" may land before
        # "section" is parsed. Must not raise or return the wrong field.
        assert _tool_input_hint("Scratchpad", {"op": "write"}) == ""

    def test_mcp_prefix_stripped(self):
        assert _tool_input_hint("mcp__server__Bash", {"command": "id"}) == "id"

    def test_empty_input(self):
        assert _tool_input_hint("Bash", None) == ""
        assert _tool_input_hint("Bash", {}) == ""

    def test_generic_first_string(self):
        assert _tool_input_hint("UnknownTool", {"opaque": "value"}) == "value"

    def test_generic_no_string(self):
        assert _tool_input_hint("UnknownTool", {"opaque": 42}) == ""

    def test_exception_returns_empty(self):
        # passing a non-dict triggers AttributeError inside; suppressed.
        assert _tool_input_hint("Bash", "not a dict") == ""


class TestToolOneline:
    def test_scratchpad_uses_dedicated_emoji(self):
        line = tool_oneline("Scratchpad", {"section": "進捗"})
        assert line.startswith("💭 Scratchpad")
        assert "進捗" in line

    def test_unregistered_tool_falls_back_to_wrench(self):
        line = tool_oneline("SecondOpinion", {"query": "x"})
        assert line.startswith("🔧 SecondOpinion")

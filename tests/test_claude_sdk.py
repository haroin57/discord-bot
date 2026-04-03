"""Tests for services/claude_sdk.py — call_with_tools with mocked SDK."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import needs_tools


class TestNeedsToolsRouting:
    """Test the routing logic used by claude_sdk."""

    def test_tool_keywords(self):
        assert needs_tools("ファイル読んで") is True
        assert needs_tools("git push") is True

    def test_chat_keywords(self):
        assert needs_tools("やっほー") is False
        assert needs_tools("今日の天気") is False


class TestCallWithTools:
    @pytest.mark.asyncio
    async def test_returns_result(self):
        mock_result = MagicMock()
        mock_result.result = "テスト応答"
        mock_result.session_id = "sess-123"

        async def fake_query(*args, **kwargs):
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "テスト応答"
            msg.session_id = "sess-123"
            yield msg

        with patch("services.claude_sdk.query", side_effect=fake_query):
            from services.claude_sdk import call_with_tools
            text, sid = await call_with_tools(
                "hello", 1, {}, raw_user_text="hello"
            )
            assert text == "テスト応答"
            assert sid == "sess-123"

    @pytest.mark.asyncio
    async def test_timeout_returns_message(self):
        async def slow_query(*args, **kwargs):
            raise TimeoutError()
            yield  # make it a generator

        with patch("services.claude_sdk.query", side_effect=slow_query):
            with patch("services.claude_sdk.CLAUDE_TIMEOUT", 1):
                from services.claude_sdk import call_with_tools
                text, sid = await call_with_tools(
                    "hello", 1, {}, raw_user_text="hello"
                )
                assert "timed out" in text.lower() or "⏰" in text

    @pytest.mark.asyncio
    async def test_error_returns_message(self):
        async def error_query(*args, **kwargs):
            raise RuntimeError("test error")
            yield

        with patch("services.claude_sdk.query", side_effect=error_query):
            from services.claude_sdk import call_with_tools
            text, sid = await call_with_tools(
                "hello", 1, {}, raw_user_text="hello"
            )
            assert "❌" in text
            assert sid is None

    @pytest.mark.asyncio
    async def test_no_response_returns_fallback(self):
        async def empty_query(*args, **kwargs):
            return
            yield

        with patch("services.claude_sdk.query", side_effect=empty_query):
            from services.claude_sdk import call_with_tools
            text, sid = await call_with_tools(
                "hello", 1, {}, raw_user_text="hello"
            )
            assert "No response" in text

    @pytest.mark.asyncio
    async def test_status_callback_called(self):
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import ToolUseBlock

        callback = AsyncMock()

        async def tool_query(*args, **kwargs):
            msg = MagicMock(spec=AssistantMessage)
            block = MagicMock(spec=ToolUseBlock)
            block.name = "Bash"
            msg.content = [block]
            yield msg

            result = MagicMock()
            result.result = "done"
            result.session_id = "s1"
            from claude_code_sdk import ResultMessage
            result.__class__ = ResultMessage
            yield result

        with patch("services.claude_sdk.query", side_effect=tool_query):
            from services.claude_sdk import call_with_tools
            await call_with_tools(
                "実行して", 1, {},
                status_callback=callback,
                raw_user_text="実行して"
            )
            callback.assert_called()

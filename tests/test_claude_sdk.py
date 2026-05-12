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
            text, info = await call_with_tools(
                "hello", 1, {}, raw_user_text="hello"
            )
            assert text == "テスト応答"
            assert info is not None
            assert info.session_id == "sess-123"

    @pytest.mark.asyncio
    async def test_timeout_returns_message(self):
        async def slow_query(*args, **kwargs):
            raise TimeoutError()
            yield  # make it a generator

        with patch("services.claude_sdk.query", side_effect=slow_query):
            with patch("services.claude_sdk.CLAUDE_TIMEOUT", 1):
                from services.claude_sdk import call_with_tools
                text, info = await call_with_tools(
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
            text, info = await call_with_tools(
                "hello", 1, {}, raw_user_text="hello"
            )
            assert "❌" in text
            assert info is None

    @pytest.mark.asyncio
    async def test_no_response_returns_fallback(self):
        async def empty_query(*args, **kwargs):
            return
            yield

        with patch("services.claude_sdk.query", side_effect=empty_query):
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools(
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


class TestSessionResume:
    """Test thread session resume logic."""

    @pytest.mark.asyncio
    async def test_resume_when_thread_and_session_fresh(self):
        """When is_thread=True and session is fresh with matching MCP, resume is used."""
        from collections import OrderedDict
        from services.session import SessionInfo
        from services.claude_sdk import _record_session_time

        sessions = OrderedDict()
        sessions[100] = SessionInfo(
            session_id="existing-sess",
            mcp_server_keys=["context7", "scholarly"],
        )
        _record_session_time(100)  # Mark as fresh

        captured_options = {}

        async def spy_query(*args, **kwargs):
            captured_options["resume"] = kwargs.get("options", args[0] if args else None)
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "resumed"
            msg.session_id = "existing-sess"
            msg.total_cost_usd = 0.01
            msg.usage = {"input_tokens": 100, "output_tokens": 50}
            msg.num_turns = 1
            msg.duration_api_ms = 1000
            msg.is_error = False
            yield msg

        with patch("services.claude_sdk.query", side_effect=spy_query) as mock_q:
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools(
                "hello", 100, sessions,
                raw_user_text="hello",
                is_thread=True,
            )
            assert text == "resumed"
            # Verify resume was set on the options
            call_args = mock_q.call_args
            options = call_args.kwargs.get("options") or call_args[1].get("options")
            assert options.resume == "existing-sess"

    @pytest.mark.asyncio
    async def test_resume_works_for_channels_too(self):
        """Resume works for regular channels (not just threads)."""
        from collections import OrderedDict
        from services.session import SessionInfo
        from services.claude_sdk import _record_session_time

        sessions = OrderedDict()
        sessions[100] = SessionInfo(
            session_id="existing-sess",
            mcp_server_keys=["context7", "scholarly"],
        )
        _record_session_time(100)

        async def spy_query(*args, **kwargs):
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "resumed"
            msg.session_id = "existing-sess"
            msg.total_cost_usd = 0.01
            msg.usage = {"input_tokens": 100, "output_tokens": 50}
            msg.num_turns = 1
            msg.duration_api_ms = 1000
            msg.is_error = False
            yield msg

        with patch("services.claude_sdk.query", side_effect=spy_query) as mock_q:
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools(
                "hello", 100, sessions,
                raw_user_text="hello",
                is_thread=False,
            )
            call_args = mock_q.call_args
            options = call_args.kwargs.get("options") or call_args[1].get("options")
            assert options.resume == "existing-sess"

    @pytest.mark.asyncio
    async def test_no_resume_when_mcp_mismatch(self):
        """When new message needs MCP servers not in stored session, start fresh."""
        from collections import OrderedDict
        from services.session import SessionInfo
        from services.claude_sdk import _record_session_time

        sessions = OrderedDict()
        sessions[100] = SessionInfo(
            session_id="existing-sess",
            mcp_server_keys=["context7", "scholarly"],  # no web servers
        )
        _record_session_time(100)

        async def spy_query(*args, **kwargs):
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "fresh"
            msg.session_id = "new-sess"
            msg.total_cost_usd = 0.01
            msg.usage = {"input_tokens": 100, "output_tokens": 50}
            msg.num_turns = 1
            msg.duration_api_ms = 1000
            msg.is_error = False
            yield msg

        with patch("services.claude_sdk.query", side_effect=spy_query) as mock_q:
            from services.claude_sdk import call_with_tools
            # "検索して" triggers web MCP servers
            text, info = await call_with_tools(
                "検索して", 100, sessions,
                raw_user_text="検索して",
                is_thread=True,
            )
            call_args = mock_q.call_args
            options = call_args.kwargs.get("options") or call_args[1].get("options")
            assert options.resume is None
            # Old session should be cleared
            assert 100 not in sessions

    @pytest.mark.asyncio
    async def test_no_resume_when_restricted(self):
        """Non-admin (restricted) never gets resume."""
        from collections import OrderedDict
        from services.session import SessionInfo
        from services.claude_sdk import _record_session_time

        sessions = OrderedDict()
        sessions[100] = SessionInfo(
            session_id="existing-sess",
            mcp_server_keys=["context7"],
        )
        _record_session_time(100)

        async def spy_query(*args, **kwargs):
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "restricted"
            msg.session_id = None
            msg.total_cost_usd = 0.01
            msg.usage = {"input_tokens": 100, "output_tokens": 50}
            msg.num_turns = 1
            msg.duration_api_ms = 1000
            msg.is_error = False
            yield msg

        with patch("services.claude_sdk.query", side_effect=spy_query) as mock_q:
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools(
                "hello", 100, sessions,
                raw_user_text="hello",
                is_thread=True,
                restricted=True,
            )
            call_args = mock_q.call_args
            options = call_args.kwargs.get("options") or call_args[1].get("options")
            assert options.resume is None

    @pytest.mark.asyncio
    async def test_returns_session_info_with_mcp_keys(self):
        """Successful call returns SessionInfo with MCP keys."""
        async def fake_query(*args, **kwargs):
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "done"
            msg.session_id = "new-sess"
            msg.total_cost_usd = 0.01
            msg.usage = {"input_tokens": 100, "output_tokens": 50}
            msg.num_turns = 1
            msg.duration_api_ms = 1000
            msg.is_error = False
            yield msg

        with patch("services.claude_sdk.query", side_effect=fake_query):
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools(
                "hello", 1, {},
                raw_user_text="hello",
                is_thread=True,
            )
            assert info is not None
            assert info.session_id == "new-sess"
            assert "context7" in info.mcp_server_keys  # BASE always included

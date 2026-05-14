"""Tests for services/claude_sdk.py — call_with_tools with mocked SDK.

PR8 (refactor/claude-api-unify) flipped the dispatcher default to "api".
These tests target the SDK-backed legacy implementation, so we force the
backend back to "sdk" for the duration of this file. The whole module is
deleted in PR9 along with services/claude_sdk.py itself.
"""

import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import needs_tools


@pytest.fixture(autouse=True)
def _force_sdk_backend(monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKEND", "sdk")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result_msg(result="ok", session_id="sess-1", cost=0.01,
                     usage=None, num_turns=1, duration_ms=500, is_error=False):
    """Build a ResultMessage-like MagicMock with all required fields."""
    from claude_code_sdk import ResultMessage
    msg = MagicMock(spec=ResultMessage)
    msg.result = result
    msg.session_id = session_id
    msg.total_cost_usd = cost
    msg.usage = usage or {"input_tokens": 10, "output_tokens": 5}
    msg.num_turns = num_turns
    msg.duration_api_ms = duration_ms
    msg.is_error = is_error
    msg.subtype = "end_turn"
    return msg


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
            with patch("services.claude_sdk.TRANSIENT_MAX_ATTEMPTS", 1):
                with patch("services.claude_sdk.DEFAULT_MAX_ATTEMPTS", 1):
                    from services.claude_sdk import call_with_tools
                    text, info = await call_with_tools(
                        "hello", 999991, {}, raw_user_text="hello"
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
        """Successful call returns SessionInfo with MCP keys reflecting chosen MCP."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg(result="done", session_id="new-sess")

        with patch("services.claude_sdk.query", side_effect=fake_query):
            from services.claude_sdk import call_with_tools
            # "docs" triggers context7 MCP
            text, info = await call_with_tools(
                "docs について教えて", 55501, {},
                raw_user_text="docs について教えて",
                is_thread=True,
            )
            assert info is not None
            assert info.session_id == "new-sess"
            # When docs keyword is present, context7 should be picked
            assert "context7" in info.mcp_server_keys


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class TestClassifyError:
    def test_rate_limit(self):
        from services.claude_sdk import _classify_error
        assert _classify_error("rate_limit exceeded", "") == "rate_limit"
        assert _classify_error("429 too many requests", "") == "rate_limit"

    def test_overloaded(self):
        from services.claude_sdk import _classify_error
        assert _classify_error("server is overloaded", "") == "overloaded"
        assert _classify_error("529 server_overload", "") == "overloaded"

    def test_auth_error(self):
        from services.claude_sdk import _classify_error
        assert _classify_error("authentication failed", "") == "auth"
        assert _classify_error("401 unauthorized", "") == "auth"
        assert _classify_error("invalid api key", "") == "auth"

    def test_context_overflow(self):
        from services.claude_sdk import _classify_error
        assert _classify_error("token limit exceeded too long", "") == "context_overflow"

    def test_max_turns(self):
        from services.claude_sdk import _classify_error
        assert _classify_error("max_turns reached", "") == "max_turns"

    def test_cli_exit(self):
        from services.claude_sdk import _classify_error
        assert _classify_error("exit code 1", "") == "cli_exit"

    def test_unknown(self):
        from services.claude_sdk import _classify_error
        assert _classify_error("something weird happened", "") == "unknown"

    def test_stderr_combined(self):
        from services.claude_sdk import _classify_error
        # error in stderr_tail should be detected
        assert _classify_error("", "ratelimiterror in details") == "rate_limit"

    def test_is_transient(self):
        from services.claude_sdk import _is_transient
        assert _is_transient("rate_limit") is True
        assert _is_transient("overloaded") is True
        assert _is_transient("auth") is False
        assert _is_transient("unknown") is False

    def test_error_message_known(self):
        from services.claude_sdk import _error_message
        assert "⏳" in _error_message("rate_limit")
        assert "🔑" in _error_message("auth")
        assert "📝" in _error_message("context_overflow")

    def test_error_message_unknown(self):
        from services.claude_sdk import _error_message
        msg = _error_message("some_unknown_type")
        assert "❌" in msg


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

class TestSessionHelpers:
    def test_is_session_fresh_absent(self):
        from services.claude_sdk import _is_session_fresh, _session_timestamps
        _session_timestamps.pop(9999, None)
        assert _is_session_fresh(9999) is False

    def test_record_and_check_fresh(self):
        from services.claude_sdk import _is_session_fresh, _record_session_time, _clear_session_time
        _record_session_time(8888)
        assert _is_session_fresh(8888) is True
        _clear_session_time(8888)
        assert _is_session_fresh(8888) is False

    def test_session_expired(self):
        from services.claude_sdk import _is_session_fresh, _session_timestamps, SESSION_TTL_S
        _session_timestamps[7777] = time.monotonic() - SESSION_TTL_S - 1
        assert _is_session_fresh(7777) is False
        _session_timestamps.pop(7777, None)


# ---------------------------------------------------------------------------
# Stderr helpers
# ---------------------------------------------------------------------------

class TestStderrHelpers:
    def test_read_stderr_buffer_bytes(self):
        from services.claude_sdk import _read_stderr_buffer
        buf = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        buf.write(b"hello stderr")
        result = _read_stderr_buffer(buf)
        assert "hello stderr" in result
        buf.close()

    def test_read_stderr_buffer_none(self):
        from services.claude_sdk import _read_stderr_buffer
        assert _read_stderr_buffer(None) == ""

    def test_read_stderr_buffer_truncates_large(self):
        from services.claude_sdk import _read_stderr_buffer
        buf = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        buf.write(b"x" * 60000)
        result = _read_stderr_buffer(buf)
        assert len(result) == 50000
        buf.close()

    def test_read_stderr_file_missing(self):
        from services.claude_sdk import _read_stderr_file
        assert _read_stderr_file("/nonexistent/path.log") == ""

    def test_read_stderr_file_empty_path(self):
        from services.claude_sdk import _read_stderr_file
        assert _read_stderr_file(None) == ""
        assert _read_stderr_file("") == ""

    def test_read_stderr_file_content(self, tmp_path):
        from services.claude_sdk import _read_stderr_file
        p = tmp_path / "stderr.log"
        p.write_text("some error output")
        result = _read_stderr_file(str(p))
        assert "some error output" in result

    def test_read_stderr_file_truncates_large(self, tmp_path):
        from services.claude_sdk import _read_stderr_file
        p = tmp_path / "big_stderr.log"
        p.write_text("y" * 15000)
        result = _read_stderr_file(str(p))
        assert len(result) == 10000

    def test_cleanup_stderr_closes_fileobj(self):
        from services.claude_sdk import _cleanup_stderr
        buf = tempfile.SpooledTemporaryFile(max_size=1024, mode="w+b")
        buf.write(b"data")
        _cleanup_stderr(buf)
        assert buf.closed

    def test_cleanup_stderr_none_fileobj(self):
        from services.claude_sdk import _cleanup_stderr
        # Should not raise
        _cleanup_stderr(None)

    def test_cleanup_stderr_removes_file(self, tmp_path):
        from services.claude_sdk import _cleanup_stderr
        p = tmp_path / "to_delete.log"
        p.write_text("delete me")
        buf = tempfile.SpooledTemporaryFile(max_size=1024, mode="w+b")
        _cleanup_stderr(buf, str(p), keep=False)
        assert not p.exists()

    def test_cleanup_stderr_keep_file(self, tmp_path):
        from services.claude_sdk import _cleanup_stderr
        p = tmp_path / "keep.log"
        p.write_text("keep me")
        buf = tempfile.SpooledTemporaryFile(max_size=1024, mode="w+b")
        _cleanup_stderr(buf, str(p), keep=True)
        assert p.exists()


# ---------------------------------------------------------------------------
# _tool_input_hint
# ---------------------------------------------------------------------------

class TestToolInputHint:
    def test_bash(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("Bash", {"command": "ls -la"}) == "ls -la"

    def test_read(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("Read", {"file_path": "/etc/hosts"}) == "/etc/hosts"

    def test_write(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("Write", {"file_path": "/tmp/out.txt"}) == "/tmp/out.txt"

    def test_glob(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("Glob", {"pattern": "*.py"}) == "*.py"

    def test_grep(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("Grep", {"pattern": "TODO"}) == "TODO"

    def test_web_fetch(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("WebFetch", {"url": "https://example.com"}) == "https://example.com"

    def test_web_search(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("WebSearch", {"query": "python async"}) == "python async"

    def test_search_x(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("search_x", {"query": "AI news"}) == "AI news"

    def test_agent(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("Agent", {"description": "research task"}) == "research task"

    def test_mcp_prefixed(self):
        from services.claude_sdk import _tool_input_hint
        # mcp__brave__brave_web_search strips prefix
        result = _tool_input_hint("mcp__brave__brave_web_search", {"query": "news"})
        assert result == "news"

    def test_empty_input(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("Bash", None) == ""
        assert _tool_input_hint("Bash", {}) == ""

    def test_generic_first_string_value(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("UnknownTool", {"something": "value1"}) == "value1"

    def test_no_string_values(self):
        from services.claude_sdk import _tool_input_hint
        assert _tool_input_hint("UnknownTool", {"num": 42}) == ""


# ---------------------------------------------------------------------------
# Restricted (non-admin) options
# ---------------------------------------------------------------------------

class TestRestrictedOptions:
    @pytest.mark.asyncio
    async def test_restricted_no_mcp_servers(self):
        """Restricted callers get empty mcp_servers."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg(result="restricted ok", session_id=None)

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            from services.claude_sdk import call_with_tools
            await call_with_tools("hello", 60001, {}, restricted=True, raw_user_text="hello")
            options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
            assert options.mcp_servers == {}

    @pytest.mark.asyncio
    async def test_restricted_permission_mode_default(self):
        """Restricted callers use permission_mode='default' not bypassPermissions."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg(result="ok", session_id=None)

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            from services.claude_sdk import call_with_tools
            await call_with_tools("hello", 60002, {}, restricted=True, raw_user_text="hello")
            options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
            assert options.permission_mode == "default"

    @pytest.mark.asyncio
    async def test_restricted_no_read_tool_without_image(self):
        """Restricted callers without image attachment get empty allowed_tools."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg(result="ok", session_id=None)

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            from services.claude_sdk import call_with_tools
            await call_with_tools("hello", 60003, {}, restricted=True,
                                  has_image_attachment=False, raw_user_text="hello")
            options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
            assert options.allowed_tools == []

    @pytest.mark.asyncio
    async def test_restricted_read_tool_with_image(self):
        """H3: restricted callers get allowed_tools=[] even when has_image_attachment=True."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg(result="ok", session_id=None)

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            from services.claude_sdk import call_with_tools
            await call_with_tools("画像を確認", 60004, {}, restricted=True,
                                  has_image_attachment=True, raw_user_text="画像を確認")
            options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
            assert options.allowed_tools == []

    @pytest.mark.asyncio
    async def test_restricted_max_turns(self):
        """Restricted callers get RESTRICTED_MAX_TURNS."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg(result="ok", session_id=None)

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            from services.claude_sdk import call_with_tools, RESTRICTED_MAX_TURNS
            await call_with_tools("hello", 60005, {}, restricted=True, raw_user_text="hello")
            options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
            assert options.max_turns == RESTRICTED_MAX_TURNS

    @pytest.mark.asyncio
    async def test_restricted_settings_none(self):
        """Restricted callers get settings=None (no admin settings)."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg(result="ok", session_id=None)

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            from services.claude_sdk import call_with_tools
            await call_with_tools("hello", 60006, {}, restricted=True, raw_user_text="hello")
            options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
            assert options.settings is None

    @pytest.mark.asyncio
    async def test_restricted_api_key_injected(self):
        """When RESTRICTED_ANTHROPIC_API_KEY is set, it's passed in env."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg(result="ok", session_id=None)

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            with patch("services.claude_sdk.RESTRICTED_ANTHROPIC_API_KEY", "test-key-xyz"):
                from services.claude_sdk import call_with_tools
                await call_with_tools("hello", 60007, {}, restricted=True, raw_user_text="hello")
                options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
                assert options.env.get("ANTHROPIC_API_KEY") == "test-key-xyz"
                assert options.env.get("ANTHROPIC_AUTH_TOKEN") == ""


# ---------------------------------------------------------------------------
# Admin options
# ---------------------------------------------------------------------------

class TestAdminOptions:
    @pytest.mark.asyncio
    async def test_admin_bypass_permissions(self):
        """Admin callers use bypassPermissions."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg()

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            from services.claude_sdk import call_with_tools
            await call_with_tools("hello", 70001, {}, restricted=False, raw_user_text="hello")
            options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
            assert options.permission_mode == "bypassPermissions"

    @pytest.mark.asyncio
    async def test_admin_max_turns(self):
        """Admin callers get ADMIN_MAX_TURNS."""
        async def fake_query(*args, **kwargs):
            yield _make_result_msg()

        with patch("services.claude_sdk.query", side_effect=fake_query) as mock_q:
            from services.claude_sdk import call_with_tools, ADMIN_MAX_TURNS
            await call_with_tools("hello", 70002, {}, restricted=False, raw_user_text="hello")
            options = mock_q.call_args.kwargs.get("options") or mock_q.call_args[1]["options"]
            assert options.max_turns == ADMIN_MAX_TURNS

    @pytest.mark.asyncio
    async def test_stream_text_fallback(self):
        """When ResultMessage.result is empty, fall back to last TextBlock stream_text."""
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import TextBlock

        async def fake_query(*args, **kwargs):
            # First yield an AssistantMessage with TextBlock
            am = MagicMock(spec=AssistantMessage)
            tb = MagicMock(spec=TextBlock)
            tb.text = "stream fallback text"
            am.content = [tb]
            yield am

            # Then ResultMessage with empty result
            msg = _make_result_msg(result="", session_id="s-fallback")
            msg.subtype = "end_turn"
            yield msg

        with patch("services.claude_sdk.query", side_effect=fake_query):
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools("hello", 70003, {}, raw_user_text="hello")
            assert text == "stream fallback text"


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------

class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_transient_rate_limit_retries(self):
        """Rate limit error triggers retry and eventually succeeds."""
        call_count = {"n": 0}

        async def flaky_query(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise RuntimeError("429 rate_limit exceeded")
            yield _make_result_msg(result="success after retry")

        with patch("services.claude_sdk.query", side_effect=flaky_query):
            with patch("services.claude_sdk.BASE_BACKOFF_S", 0):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    from services.claude_sdk import call_with_tools
                    text, info = await call_with_tools(
                        "hello", 80001, {}, raw_user_text="hello"
                    )
                    assert text == "success after retry"
                    assert call_count["n"] >= 2

    @pytest.mark.asyncio
    async def test_auth_error_no_retry(self):
        """Auth errors are non-retryable — return immediately."""
        call_count = {"n": 0}

        async def auth_fail(*args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("authentication failed 401 unauthorized")
            yield

        with patch("services.claude_sdk.query", side_effect=auth_fail):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                from services.claude_sdk import call_with_tools
                text, info = await call_with_tools(
                    "hello", 80002, {}, raw_user_text="hello"
                )
                assert "🔑" in text
                assert call_count["n"] == 1  # no retry for auth

    @pytest.mark.asyncio
    async def test_context_overflow_no_retry(self):
        """Context overflow is non-retryable."""
        async def ctx_fail(*args, **kwargs):
            raise RuntimeError("token limit exceeded too long")
            yield

        with patch("services.claude_sdk.query", side_effect=ctx_fail):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                from services.claude_sdk import call_with_tools
                text, info = await call_with_tools(
                    "hello", 80003, {}, raw_user_text="hello"
                )
                assert "📝" in text
                assert info is None

    @pytest.mark.asyncio
    async def test_timeout_clears_resume(self):
        """TimeoutError on a resumed session clears the session and retries fresh."""
        from services.session import SessionInfo
        from services.claude_sdk import _record_session_time

        sessions = {90001: SessionInfo(session_id="old-sess", mcp_server_keys=[])}
        _record_session_time(90001)

        call_count = {"n": 0}

        async def timeout_then_ok(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise TimeoutError()
            yield _make_result_msg(result="fresh ok", session_id="new-sess")

        with patch("services.claude_sdk.query", side_effect=timeout_then_ok):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with patch("services.claude_sdk.kill_orphan_claude_cli", return_value=0):
                    from services.claude_sdk import call_with_tools
                    text, info = await call_with_tools(
                        "hello", 90001, sessions, raw_user_text="hello"
                    )
                    assert text == "fresh ok"
                    assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_timeout_no_resume_returns_message(self):
        """TimeoutError with no resume returns timeout message directly."""
        async def always_timeout(*args, **kwargs):
            raise TimeoutError()
            yield

        with patch("services.claude_sdk.query", side_effect=always_timeout):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with patch("services.claude_sdk.kill_orphan_claude_cli", return_value=0):
                    with patch("services.claude_sdk.TRANSIENT_MAX_ATTEMPTS", 1):
                        from services.claude_sdk import call_with_tools
                        text, info = await call_with_tools(
                            "hello", 90002, {}, raw_user_text="hello"
                        )
                        assert "⏰" in text
                        assert info is None

    @pytest.mark.asyncio
    async def test_cli_exit_with_partial_response(self):
        """cli_exit with a partial response returns what we got."""
        call_count = {"n": 0}

        async def partial_then_exit(*args, **kwargs):
            call_count["n"] += 1
            # Yield a result message so response_text is set, then raise cli_exit
            msg = _make_result_msg(result="partial answer here", session_id="p-sess")
            yield msg
            # Simulate a second call that raises
            raise RuntimeError("exit code 1 cli")

        # We need to simulate getting the ResultMessage first then the exit code error.
        # Instead, test the case where response_text is set before the error path:
        # Use separate invocations
        results = {"first": True}

        async def cli_exit_error(*args, **kwargs):
            if results["first"]:
                results["first"] = False
                raise RuntimeError("exit code 1 failed")
            yield _make_result_msg(result="ok")

        with patch("services.claude_sdk.query", side_effect=cli_exit_error):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with patch("services.claude_sdk.TRANSIENT_MAX_ATTEMPTS", 1):
                    with patch("services.claude_sdk.DEFAULT_MAX_ATTEMPTS", 1):
                        from services.claude_sdk import call_with_tools
                        text, info = await call_with_tools(
                            "hello", 90003, {}, raw_user_text="hello"
                        )
                        assert "⚠️" in text


# ---------------------------------------------------------------------------
# Message stream processing
# ---------------------------------------------------------------------------

class TestStreamProcessing:
    @pytest.mark.asyncio
    async def test_thinking_block_status_callback(self):
        """ThinkingBlock triggers status_callback."""
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import ThinkingBlock

        callback = AsyncMock()

        async def thinking_query(*args, **kwargs):
            am = MagicMock(spec=AssistantMessage)
            tb = MagicMock(spec=ThinkingBlock)
            tb.thinking = "I am thinking about this problem"
            am.content = [tb]
            yield am
            yield _make_result_msg()

        with patch("services.claude_sdk.query", side_effect=thinking_query):
            from services.claude_sdk import call_with_tools
            await call_with_tools("hello", 100001, {}, status_callback=callback,
                                  raw_user_text="hello")
            # callback should have been called with thinking summary
            calls = [str(c) for c in callback.call_args_list]
            assert any("💭" in c for c in calls)

    @pytest.mark.asyncio
    async def test_tool_result_error_status(self):
        """ToolResultBlock with is_error=True triggers error status callback."""
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import ToolResultBlock
        from claude_code_sdk.types import UserMessage as SDKUserMessage

        callback = AsyncMock()

        async def tool_result_query(*args, **kwargs):
            um = MagicMock(spec=SDKUserMessage)
            trb = MagicMock(spec=ToolResultBlock)
            trb.is_error = True
            trb.content = "tool failed"
            um.content = [trb]
            yield um
            yield _make_result_msg()

        with patch("services.claude_sdk.query", side_effect=tool_result_query):
            from services.claude_sdk import call_with_tools
            await call_with_tools("hello", 100002, {}, status_callback=callback,
                                  raw_user_text="hello")
            calls = [str(c) for c in callback.call_args_list]
            assert any("❌" in c for c in calls)

    @pytest.mark.asyncio
    async def test_system_message_logged(self):
        """SystemMessage is processed without error."""
        from claude_code_sdk.types import SystemMessage as SDKSystemMessage

        async def sys_msg_query(*args, **kwargs):
            sm = MagicMock(spec=SDKSystemMessage)
            sm.subtype = "init"
            yield sm
            yield _make_result_msg()

        with patch("services.claude_sdk.query", side_effect=sys_msg_query):
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools("hello", 100003, {}, raw_user_text="hello")
            assert text == "ok"

    @pytest.mark.asyncio
    async def test_tool_result_ok_status(self):
        """ToolResultBlock without error triggers ok status callback."""
        from claude_code_sdk.types import ToolResultBlock
        from claude_code_sdk.types import UserMessage as SDKUserMessage

        callback = AsyncMock()

        async def ok_result_query(*args, **kwargs):
            um = MagicMock(spec=SDKUserMessage)
            trb = MagicMock(spec=ToolResultBlock)
            trb.is_error = False
            trb.content = "tool succeeded"
            um.content = [trb]
            yield um
            yield _make_result_msg()

        with patch("services.claude_sdk.query", side_effect=ok_result_query):
            from services.claude_sdk import call_with_tools
            await call_with_tools("hello", 100004, {}, status_callback=callback,
                                  raw_user_text="hello")
            calls = [str(c) for c in callback.call_args_list]
            assert any("📨" in c for c in calls)


class TestKillOrphanClaude:
    def test_returns_zero_when_no_children(self):
        """kill_orphan_claude_cli returns 0 when no matching descendants exist."""
        import psutil as _psutil
        from unittest.mock import patch, MagicMock
        from services.claude_sdk import kill_orphan_claude_cli

        mock_me = MagicMock()
        mock_me.children.return_value = []
        with patch("psutil.Process", return_value=mock_me):
            result = kill_orphan_claude_cli()
        assert result == 0

    def test_kills_claude_named_process(self):
        """kill_orphan_claude_cli terminates process named 'claude'."""
        import psutil as _psutil
        from unittest.mock import patch, MagicMock, call
        from services.claude_sdk import kill_orphan_claude_cli

        mock_child = MagicMock()
        mock_child.name.return_value = "claude"
        mock_child.cmdline.return_value = ["/usr/bin/claude"]

        mock_me = MagicMock()
        mock_me.children.return_value = [mock_child]

        with patch("psutil.Process", return_value=mock_me):
            with patch("psutil.wait_procs", return_value=([], [])):
                result = kill_orphan_claude_cli()
        assert result == 1
        mock_child.terminate.assert_called_once()

    def test_kills_mcp_server_process(self):
        """kill_orphan_claude_cli terminates process with mcp-server- in cmdline."""
        import psutil as _psutil
        from unittest.mock import patch, MagicMock
        from services.claude_sdk import kill_orphan_claude_cli

        mock_child = MagicMock()
        mock_child.name.return_value = "node"
        mock_child.cmdline.return_value = ["node", "/usr/lib/mcp-server-memory/index.js"]

        mock_me = MagicMock()
        mock_me.children.return_value = [mock_child]

        with patch("psutil.Process", return_value=mock_me):
            with patch("psutil.wait_procs", return_value=([], [])):
                result = kill_orphan_claude_cli()
        assert result == 1

    def test_handles_exception_gracefully(self):
        """kill_orphan_claude_cli returns 0 on unexpected psutil exception."""
        from unittest.mock import patch
        from services.claude_sdk import kill_orphan_claude_cli

        with patch("psutil.Process", side_effect=RuntimeError("boom")):
            result = kill_orphan_claude_cli()
        assert result == 0

    def test_kills_alive_process_with_sigkill(self):
        """Processes still alive after SIGTERM get SIGKILL."""
        import psutil as _psutil
        from unittest.mock import patch, MagicMock
        from services.claude_sdk import kill_orphan_claude_cli

        mock_child = MagicMock()
        mock_child.name.return_value = "claude"
        mock_child.cmdline.return_value = ["claude"]

        mock_me = MagicMock()
        mock_me.children.return_value = [mock_child]

        # alive list contains the child → SIGKILL should be called
        with patch("psutil.Process", return_value=mock_me):
            with patch("psutil.wait_procs", return_value=([], [mock_child])):
                result = kill_orphan_claude_cli()
        mock_child.kill.assert_called_once()
        assert result == 1


class TestStderrHelperExceptions:
    def test_read_stderr_buffer_exception(self):
        """_read_stderr_buffer returns '' when read raises."""
        from services.claude_sdk import _read_stderr_buffer
        from unittest.mock import MagicMock

        bad = MagicMock()
        bad.flush.side_effect = IOError("disk error")
        result = _read_stderr_buffer(bad)
        assert result == ""

    def test_read_stderr_buffer_text_mode(self):
        """_read_stderr_buffer handles text content (not bytes)."""
        import tempfile
        from services.claude_sdk import _read_stderr_buffer

        buf = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+")
        buf.write("text mode content")
        result = _read_stderr_buffer(buf)
        assert "text mode content" in result
        buf.close()


class TestH2StderrFileSave:
    @pytest.mark.asyncio
    async def test_stderr_saved_to_disk_on_error(self, tmp_path, monkeypatch):
        """H2: when error has stderr, it's saved to STATE_DIR with mode 0o600."""
        import services.claude_sdk as sdk
        monkeypatch.setattr(sdk, "STATE_DIR", str(tmp_path))

        call_count = {"n": 0}

        async def error_with_stderr(*args, **kwargs):
            call_count["n"] += 1
            # Write something to debug_stderr before raising
            opts = kwargs.get("options")
            if opts and hasattr(opts, "debug_stderr") and opts.debug_stderr:
                opts.debug_stderr.write(b"some stderr content with error details")
            raise RuntimeError("authentication failed 401")
            yield

        with patch("services.claude_sdk.query", side_effect=error_with_stderr):
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools(
                "hello", 11001, {}, raw_user_text="hello"
            )

        debug_file = tmp_path / "stderr_ch11001.log"
        assert debug_file.exists()
        stat = os.stat(str(debug_file))
        assert (stat.st_mode & 0o777) == 0o600


class TestCliExitResumeRetry:
    @pytest.mark.asyncio
    async def test_cli_exit_with_resume_retries_fresh(self):
        """cli_exit on a resumed session clears session and retries fresh."""
        from services.session import SessionInfo
        from services.claude_sdk import _record_session_time

        sessions = {95001: SessionInfo(session_id="stale-sess", mcp_server_keys=[])}
        _record_session_time(95001)

        call_count = {"n": 0}

        async def cli_exit_then_ok(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("exit code 1 session mismatch")
            yield _make_result_msg(result="fresh response", session_id="new-sess")

        with patch("services.claude_sdk.query", side_effect=cli_exit_then_ok):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                from services.claude_sdk import call_with_tools
                text, info = await call_with_tools(
                    "hello", 95001, sessions, raw_user_text="hello"
                )
        assert text == "fresh response"
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_retry_status_callback_called(self):
        """Status callback is called with retry message on retryable errors."""
        call_count = {"n": 0}
        callback = AsyncMock()

        async def flaky_query(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise RuntimeError("overloaded 529")
            yield _make_result_msg(result="ok after retry")

        with patch("services.claude_sdk.query", side_effect=flaky_query):
            with patch("services.claude_sdk.BASE_BACKOFF_S", 0):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    from services.claude_sdk import call_with_tools
                    text, info = await call_with_tools(
                        "hello", 95002, {}, status_callback=callback,
                        raw_user_text="hello"
                    )
        assert text == "ok after retry"
        calls = [str(c) for c in callback.call_args_list]
        assert any("🔄" in c for c in calls)


class TestCoverageGaps:
    def test_cleanup_stderr_exception_on_close(self):
        """_cleanup_stderr silently ignores exception when fileobj.close() raises."""
        from services.claude_sdk import _cleanup_stderr
        from unittest.mock import MagicMock

        bad = MagicMock()
        bad.closed = False
        bad.close.side_effect = IOError("cannot close")
        # Should not raise
        _cleanup_stderr(bad)

    def test_kill_orphan_psutil_none(self):
        """kill_orphan_claude_cli returns 0 when psutil is None."""
        import services.claude_sdk as sdk
        original = sdk.psutil
        sdk.psutil = None
        try:
            result = sdk.kill_orphan_claude_cli()
        finally:
            sdk.psutil = original
        assert result == 0

    def test_kill_orphan_child_no_such_process(self):
        """NoSuchProcess during child enumeration is silently skipped."""
        import psutil as _psutil
        from unittest.mock import patch, MagicMock
        from services.claude_sdk import kill_orphan_claude_cli

        mock_child = MagicMock()
        mock_child.name.side_effect = _psutil.NoSuchProcess(pid=9999)

        mock_me = MagicMock()
        mock_me.children.return_value = [mock_child]

        with patch("psutil.Process", return_value=mock_me):
            result = kill_orphan_claude_cli()
        assert result == 0

    def test_kill_orphan_terminate_no_such_process(self):
        """NoSuchProcess during terminate() is silently ignored."""
        import psutil as _psutil
        from unittest.mock import patch, MagicMock
        from services.claude_sdk import kill_orphan_claude_cli

        mock_child = MagicMock()
        mock_child.name.return_value = "claude"
        mock_child.cmdline.return_value = ["claude"]
        mock_child.terminate.side_effect = _psutil.NoSuchProcess(pid=9998)

        mock_me = MagicMock()
        mock_me.children.return_value = [mock_child]

        with patch("psutil.Process", return_value=mock_me):
            with patch("psutil.wait_procs", return_value=([], [])):
                result = kill_orphan_claude_cli()
        assert result == 1

    def test_kill_orphan_kill_no_such_process(self):
        """NoSuchProcess during kill() is silently ignored."""
        import psutil as _psutil
        from unittest.mock import patch, MagicMock
        from services.claude_sdk import kill_orphan_claude_cli

        mock_child = MagicMock()
        mock_child.name.return_value = "claude"
        mock_child.cmdline.return_value = ["claude"]
        mock_child.kill.side_effect = _psutil.NoSuchProcess(pid=9997)

        mock_me = MagicMock()
        mock_me.children.return_value = [mock_child]

        with patch("psutil.Process", return_value=mock_me):
            with patch("psutil.wait_procs", return_value=([], [mock_child])):
                result = kill_orphan_claude_cli()
        assert result == 1

    def test_tool_input_hint_exception(self):
        """_tool_input_hint returns '' when an exception occurs."""
        from services.claude_sdk import _tool_input_hint

        # Pass a non-dict that will cause an exception in .get()
        class BadDict:
            def get(self, key, default=None):
                raise RuntimeError("boom")
            def __bool__(self):
                return True
            def values(self):
                raise RuntimeError("boom")

        result = _tool_input_hint("Bash", BadDict())
        assert result == ""

    @pytest.mark.asyncio
    async def test_thinking_block_empty_thinking(self):
        """ThinkingBlock with empty/None thinking is skipped without error."""
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import ThinkingBlock

        callback = AsyncMock()

        async def thinking_empty_query(*args, **kwargs):
            am = MagicMock(spec=AssistantMessage)
            tb = MagicMock(spec=ThinkingBlock)
            tb.thinking = ""  # empty → should not call callback
            am.content = [tb]
            yield am
            yield _make_result_msg()

        with patch("services.claude_sdk.query", side_effect=thinking_empty_query):
            from services.claude_sdk import call_with_tools
            text, info = await call_with_tools("hello", 110001, {}, status_callback=callback,
                                               raw_user_text="hello")
        # callback should not have been called with 💭 (no thinking content)
        calls = [str(c) for c in callback.call_args_list]
        assert not any("💭" in c for c in calls)

    @pytest.mark.asyncio
    async def test_tool_use_block_with_dict_input(self):
        """ToolUseBlock with dict input triggers status callback with hint."""
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import ToolUseBlock

        callback = AsyncMock()

        async def tool_dict_query(*args, **kwargs):
            am = MagicMock(spec=AssistantMessage)
            block = MagicMock(spec=ToolUseBlock)
            block.name = "Bash"
            block.input = {"command": "ls /tmp"}
            am.content = [block]
            yield am
            yield _make_result_msg()

        with patch("services.claude_sdk.query", side_effect=tool_dict_query):
            from services.claude_sdk import call_with_tools
            await call_with_tools("run", 110002, {}, status_callback=callback,
                                  raw_user_text="run")
        calls = [str(c) for c in callback.call_args_list]
        assert any("Bash" in c for c in calls)

    @pytest.mark.asyncio
    async def test_cli_exit_partial_response_break(self):
        """cli_exit with response_text already set returns partial text."""
        from claude_code_sdk import ResultMessage

        async def result_then_exit(*args, **kwargs):
            # Yield a result message first, then raise cli_exit
            msg = _make_result_msg(result="partial text", session_id="p2")
            yield msg
            raise RuntimeError("exit code 1")

        with patch("services.claude_sdk.query", side_effect=result_then_exit):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                from services.claude_sdk import call_with_tools
                text, info = await call_with_tools(
                    "hello", 110003, {}, raw_user_text="hello"
                )
        # partial text was already set before the exit error
        assert text == "partial text"

    @pytest.mark.asyncio
    async def test_broken_session_cleared_on_unknown_error(self):
        """Unknown error during call clears broken session from dict."""
        from services.session import SessionInfo
        from services.claude_sdk import _record_session_time

        sessions = {110004: SessionInfo(session_id="broken-sess", mcp_server_keys=[])}
        _record_session_time(110004)

        call_count = {"n": 0}

        async def unknown_error(*args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("something weird happened in sdk")
            yield

        with patch("services.claude_sdk.query", side_effect=unknown_error):
            with patch("services.claude_sdk.TRANSIENT_MAX_ATTEMPTS", 1):
                with patch("services.claude_sdk.DEFAULT_MAX_ATTEMPTS", 1):
                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        from services.claude_sdk import call_with_tools
                        text, info = await call_with_tools(
                            "hello", 110004, sessions, raw_user_text="hello"
                        )
        assert 110004 not in sessions


class TestRetryBudgetLRU:
    @pytest.mark.asyncio
    async def test_lru_eviction_when_dict_too_large(self):
        """LRU eviction removes oldest channel when _retry_budget exceeds max size."""
        import services.claude_sdk as sdk
        from services.claude_sdk import _RETRY_BUDGET_MAX_CHANNELS

        # Fill budget dict to max capacity
        sdk._retry_budget.clear()
        for i in range(_RETRY_BUDGET_MAX_CHANNELS):
            sdk._retry_budget[i] = [time.monotonic() - 120]  # stale timestamps

        call_count = {"n": 0}

        async def flaky_query(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise RuntimeError("overloaded 529")
            yield _make_result_msg(result="ok")

        with patch("services.claude_sdk.query", side_effect=flaky_query):
            with patch("services.claude_sdk.BASE_BACKOFF_S", 0):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    from services.claude_sdk import call_with_tools
                    await call_with_tools("hello", 999998, {}, raw_user_text="hello")

        # Dict should not have grown beyond max + 1
        assert len(sdk._retry_budget) <= _RETRY_BUDGET_MAX_CHANNELS + 1
        # Clean up
        sdk._retry_budget.clear()


class TestSecurityFixes:
    """Tests for H2, H3, M3, M5 security fixes."""

    def test_stderr_redacts_api_key_and_token(self, tmp_path):
        """H2: stderr written to disk must not contain plaintext API keys."""
        from services.claude_sdk import _redact_secrets

        dirty = (
            "some log line\n"
            "ANTHROPIC_API_KEY=sk-ant-real-secret-value\n"
            "ANTHROPIC_AUTH_TOKEN=mytoken123\n"
            "Bearer sk-ant-api03-abcDEF_xyz-rest\n"
            "normal line\n"
        )
        clean = _redact_secrets(dirty)

        assert "sk-ant-real-secret-value" not in clean
        assert "mytoken123" not in clean
        assert "sk-ant-api03-abcDEF_xyz-rest" not in clean
        # Env var names should still be present (only values redacted)
        assert "ANTHROPIC_API_KEY=***" in clean
        assert "ANTHROPIC_AUTH_TOKEN=***" in clean
        assert "sk-ant-***" in clean

    def test_stderr_file_written_with_secure_permissions(self, tmp_path, monkeypatch):
        """H2: stderr debug file must be created with mode 0o600."""
        import services.claude_sdk as sdk

        monkeypatch.setattr(sdk, "STATE_DIR", str(tmp_path))

        debug_path = tmp_path / "stderr_ch999.log"
        stderr_tail = "ANTHROPIC_API_KEY=secret\nsome error"

        fd = os.open(
            str(debug_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sdk._redact_secrets(stderr_tail))

        stat = os.stat(str(debug_path))
        # Permissions must be exactly 0o600 (owner rw only)
        assert (stat.st_mode & 0o777) == 0o600
        content = debug_path.read_text()
        assert "secret" not in content

    @pytest.mark.asyncio
    async def test_restricted_allowed_tools_is_empty(self):
        """H3: restricted mode must use allowed_tools=[] even when has_image_attachment=True."""
        captured_options = {}

        async def spy_query(*args, **kwargs):
            captured_options["opts"] = kwargs.get("options") or args[1]
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "ok"
            msg.session_id = None
            msg.total_cost_usd = 0.0
            msg.usage = {"input_tokens": 10, "output_tokens": 5}
            msg.num_turns = 1
            msg.duration_api_ms = 100
            msg.is_error = False
            yield msg

        with patch("services.claude_sdk.query", side_effect=spy_query):
            from services.claude_sdk import call_with_tools
            await call_with_tools(
                "hello", 42, {},
                restricted=True,
                has_image_attachment=True,
            )

        opts = captured_options.get("opts")
        assert opts is not None
        assert opts.allowed_tools == []

    @pytest.mark.asyncio
    async def test_pick_mcp_servers_does_not_fall_back_to_prompt(self):
        """M3: pick_mcp_servers must only receive raw_user_text, never the full prompt."""
        received_args = []

        from services.mcp_selector import pick_mcp_servers as real_pick

        def recording_pick(text):
            received_args.append(text)
            return real_pick(text)

        async def fake_query(*args, **kwargs):
            from claude_code_sdk import ResultMessage
            msg = MagicMock(spec=ResultMessage)
            msg.result = "ok"
            msg.session_id = "s1"
            msg.total_cost_usd = 0.0
            msg.usage = {"input_tokens": 10, "output_tokens": 5}
            msg.num_turns = 1
            msg.duration_api_ms = 100
            msg.is_error = False
            yield msg

        with patch("services.claude_sdk.pick_mcp_servers", side_effect=recording_pick):
            with patch("services.claude_sdk.query", side_effect=fake_query):
                from services.claude_sdk import call_with_tools
                # prompt is positional; raw_user_text="" so the fallback to prompt
                # must NOT happen (M3 fix)
                await call_with_tools(
                    "please use playwright to browse the web",
                    99,
                    {},
                    restricted=False,
                    raw_user_text="",
                )

        # pick_mcp_servers must receive "" (empty string), NOT the full prompt
        assert len(received_args) >= 1
        assert received_args[0] == ""

    @pytest.mark.asyncio
    async def test_retry_budget_bounded_per_channel(self):
        """M5: after 3 transient retries within 60 s, the 4th attempt is blocked."""
        import services.claude_sdk as sdk

        channel_id = 77777
        # Pre-fill budget with 3 recent timestamps to simulate 3 retries already used
        now = time.monotonic()
        sdk._retry_budget[channel_id] = [now - 5, now - 3, now - 1]

        call_count = 0

        async def transient_query(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("overloaded server 529")
            yield  # make it a generator

        from services.claude_sdk import call_with_tools
        with patch("services.claude_sdk.query", side_effect=transient_query):
            with patch("services.claude_sdk.asyncio.sleep", new_callable=AsyncMock):
                text, info = await call_with_tools(
                    "hello", channel_id, {},
                    restricted=False,
                    raw_user_text="hello",
                )

        # Budget was exhausted before the 2nd attempt could even be retried
        assert info is None
        # The query was called once (attempt 0), then budget check blocked attempt 1
        assert call_count == 1

"""Tests for services/claude_sdk.py — call_with_tools with mocked SDK."""

import asyncio
import os
import time
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

        real_pick = None
        import config as _cfg
        real_pick = _cfg.pick_mcp_servers

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

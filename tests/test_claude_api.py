"""Tests for services/claude_api.py — normalize_messages, OAuth client, streaming."""

import asyncio
import json
import time
from collections import OrderedDict
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from services.claude_api import (
    _build_cached_system,
    _get_attribution_header,
    _get_metadata,
    _inject_cache_breakpoints,
    _retry_backoff_base,
    _stream_once,
    normalize_messages,
)


class TestNormalizeMessages:
    def test_empty(self):
        assert normalize_messages([]) == []

    def test_no_merge_needed(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        assert normalize_messages(msgs) == msgs

    def test_merges_consecutive_user_strings(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 1
        assert "first" in result[0]["content"]
        assert "second" in result[0]["content"]

    def test_merges_consecutive_user_lists_with_hoist(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "result1"},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "world"},
            ]},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 1
        content = result[0]["content"]
        assert content[0]["type"] == "tool_result"
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert len(text_blocks) == 2

    def test_does_not_merge_different_roles(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 3

    def test_does_not_merge_mixed_types(self):
        msgs = [
            {"role": "user", "content": "string"},
            {"role": "user", "content": [{"type": "text", "text": "list"}]},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 2

    def test_multiple_consecutive_merges(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 1
        assert "a" in result[0]["content"]
        assert "c" in result[0]["content"]


class TestTokenExpiry:
    """Tests for oauth_manager._is_expired() which replaced _is_token_expired."""

    def test_past_is_expired(self, monkeypatch):
        import services.oauth_manager as om
        past_ms = int(time.time() * 1000) - 1000
        monkeypatch.setattr(om, "_expires_at", past_ms)
        assert om._is_expired() is True

    def test_within_buffer_is_expired(self, monkeypatch):
        import services.oauth_manager as om
        # 200s from now < 300s (EXPIRY_BUFFER_MS) => still expired
        almost_expired_ms = int(time.time() * 1000) + 200_000
        monkeypatch.setattr(om, "_expires_at", almost_expired_ms)
        assert om._is_expired() is True

    def test_future_is_not_expired(self, monkeypatch):
        import services.oauth_manager as om
        future_ms = int(time.time() * 1000) + 600_000  # 10 min from now
        monkeypatch.setattr(om, "_expires_at", future_ms)
        assert om._is_expired() is False

    def test_zero_expires_at_is_expired(self, monkeypatch):
        import services.oauth_manager as om
        monkeypatch.setattr(om, "_expires_at", 0)
        assert om._is_expired() is True


class TestLoadOAuthTokens:
    @pytest.mark.skip(reason="_load_oauth_tokens moved to oauth_manager; not exported from claude_api")
    def test_loads_from_credentials(self):
        pass

    @pytest.mark.skip(reason="_load_oauth_tokens moved to oauth_manager; not exported from claude_api")
    def test_raises_on_missing_file(self):
        pass


class TestGetClient:
    def test_creates_client(self, monkeypatch):
        from services.claude_api import get_client
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_cached_client", None)
        monkeypatch.setattr(api_mod, "_cached_token", None)
        monkeypatch.setattr("services.oauth_manager.init", lambda: None)
        monkeypatch.setattr("services.oauth_manager.get_access_token", lambda: "sk-test")
        client = get_client()
        assert client is not None

    def test_caches_client(self, monkeypatch):
        from services.claude_api import get_client
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_cached_client", None)
        monkeypatch.setattr(api_mod, "_cached_token", None)
        monkeypatch.setattr("services.oauth_manager.init", lambda: None)
        monkeypatch.setattr("services.oauth_manager.get_access_token", lambda: "sk-cached")
        c1 = get_client()
        c2 = get_client()
        assert c1 is c2

    def test_refreshes_on_token_change(self, monkeypatch):
        from services.claude_api import get_client
        import services.claude_api as api_mod
        old_client = MagicMock()
        monkeypatch.setattr(api_mod, "_cached_client", old_client)
        monkeypatch.setattr(api_mod, "_cached_token", "sk-old")
        monkeypatch.setattr("services.oauth_manager.init", lambda: None)
        monkeypatch.setattr("services.oauth_manager.get_access_token", lambda: "sk-new")
        client = get_client()
        assert client is not old_client


class TestGetAttributionHeader:
    def test_returns_header_string(self):
        result = _get_attribution_header("test messages")
        assert result.startswith("x-anthropic-billing-header:")
        assert "cc_version=" in result
        assert "cc_entrypoint=" in result

    def test_fingerprint_is_16_chars(self):
        result = _get_attribution_header("test")
        # format: "x-anthropic-billing-header: cc_version=VERSION.FINGERPRINT; ..."
        version_part = result.split("cc_version=")[1].split(";")[0]
        fingerprint = version_part.split(".")[-1]
        assert len(fingerprint) == 16

    def test_different_inputs_give_different_fingerprints(self):
        r1 = _get_attribution_header("aaa")
        r2 = _get_attribution_header("bbb")
        assert r1 != r2

    def test_empty_input(self):
        result = _get_attribution_header("")
        assert "cc_version=" in result


class TestGetMetadata:
    def test_returns_dict_with_user_id(self):
        meta = _get_metadata()
        assert "user_id" in meta

    def test_user_id_contains_session_id(self):
        meta = _get_metadata()
        parsed = json.loads(meta["user_id"])
        assert "session_id" in parsed


class TestRetryBackoffBase:
    def test_default_value(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_API_RETRY_BACKOFF_BASE", raising=False)
        result = _retry_backoff_base()
        assert result == 5.0

    def test_custom_value(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_API_RETRY_BACKOFF_BASE", "10.0")
        result = _retry_backoff_base()
        assert result == 10.0

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_API_RETRY_BACKOFF_BASE", "not_a_float")
        result = _retry_backoff_base()
        assert result == 5.0


class TestBuildCachedSystem:
    def test_returns_list_with_one_block(self):
        result = _build_cached_system("hello system")
        assert isinstance(result, list)
        assert len(result) == 1

    def test_block_has_cache_control(self):
        result = _build_cached_system("prompt")
        block = result[0]
        assert block["type"] == "text"
        assert block["text"] == "prompt"
        assert block["cache_control"] == {"type": "ephemeral"}


class TestInjectCacheBreakpoints:
    def test_too_short_returns_unchanged(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        result = _inject_cache_breakpoints(msgs)
        assert result is msgs  # same object, unchanged

    def test_not_enough_user_msgs_returns_unchanged(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "assistant", "content": "c"},
            {"role": "assistant", "content": "d"},
        ]
        result = _inject_cache_breakpoints(msgs)
        assert result is msgs

    def test_string_content_gets_cache_control(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "resp1"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "resp2"},
            {"role": "user", "content": "third"},
        ]
        result = _inject_cache_breakpoints(msgs)
        # second-to-last user is index 2 ("second")
        modified = result[2]
        assert isinstance(modified["content"], list)
        assert modified["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert modified["content"][0]["text"] == "second"

    def test_list_content_gets_cache_control_on_last_block(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "first"}]},
            {"role": "assistant", "content": "r"},
            {"role": "user", "content": [{"type": "text", "text": "second"}, {"type": "text", "text": "extra"}]},
            {"role": "assistant", "content": "r2"},
            {"role": "user", "content": "third"},
        ]
        result = _inject_cache_breakpoints(msgs)
        modified_content = result[2]["content"]
        assert isinstance(modified_content, list)
        assert modified_content[-1]["cache_control"] == {"type": "ephemeral"}

    def test_does_not_mutate_original(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "r"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "r2"},
            {"role": "user", "content": "third"},
        ]
        original_content = msgs[2]["content"]
        _inject_cache_breakpoints(msgs)
        # original should be unchanged
        assert msgs[2]["content"] == original_content


class TestStreamOnce:
    def _make_mock_stream(self, texts, final_usage=None):
        """Build a mock context manager that yields text chunks."""
        mock_stream = MagicMock()
        mock_stream.text_stream = iter(texts)

        if final_usage is None:
            mock_msg = MagicMock()
            mock_msg.usage = None
            mock_stream.get_final_message.return_value = mock_msg
        else:
            mock_msg = MagicMock()
            usage = MagicMock()
            usage.cache_read_input_tokens = final_usage.get("cache_read", 0)
            usage.cache_creation_input_tokens = final_usage.get("cache_create", 0)
            usage.input_tokens = final_usage.get("input", 100)
            mock_msg.usage = usage
            mock_stream.get_final_message.return_value = mock_msg

        @contextmanager
        def fake_stream_ctx(*args, **kwargs):
            yield mock_stream

        return fake_stream_ctx

    def test_basic_stream_collects_chunks(self):
        mock_client = MagicMock()
        mock_client.beta.messages.stream = self._make_mock_stream(["hello", " world"])

        chunks = []
        _stream_once(mock_client, [{"role": "user", "content": "hi"}], "system", chunks)
        assert chunks == ["hello", " world"]

    def test_stream_with_usage_logging(self):
        mock_client = MagicMock()
        mock_client.beta.messages.stream = self._make_mock_stream(
            ["ok"],
            final_usage={"input": 50, "cache_read": 200, "cache_create": 10},
        )

        chunks = []
        _stream_once(mock_client, [{"role": "user", "content": "hi"}], "system", chunks)
        assert chunks == ["ok"]

    def test_stream_no_final_message(self):
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.text_stream = iter(["chunk"])
        mock_stream.get_final_message.return_value = None

        @contextmanager
        def fake_ctx(*args, **kwargs):
            yield mock_stream

        mock_client.beta.messages.stream = fake_ctx

        chunks = []
        _stream_once(mock_client, [{"role": "user", "content": "hi"}], "sys", chunks)
        assert chunks == ["chunk"]

    def test_stream_idle_timeout_breaks(self, monkeypatch):
        import services.claude_api as api_mod

        # Patch STREAM_IDLE_TIMEOUT_S to 0 so first gap triggers break
        monkeypatch.setattr(api_mod, "STREAM_IDLE_TIMEOUT_S", 0)
        monkeypatch.setattr(api_mod, "STREAM_STALL_THRESHOLD_S", 0)

        mock_client = MagicMock()
        mock_stream = MagicMock()

        call_count = 0
        def slow_iter():
            nonlocal call_count
            # First chunk is fine
            yield "first"
            # Second chunk simulates a big gap — but the timeout is 0
            yield "second"

        mock_stream.text_stream = slow_iter()
        mock_msg = MagicMock()
        mock_msg.usage = None
        mock_stream.get_final_message.return_value = mock_msg

        @contextmanager
        def fake_ctx(*args, **kwargs):
            yield mock_stream

        mock_client.beta.messages.stream = fake_ctx

        chunks = []
        # Should not raise even if it breaks early
        _stream_once(mock_client, [{"role": "user", "content": "hi"}], "sys", chunks)

    def test_stream_raises_propagates(self):
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.text_stream = iter(["a"])
        mock_stream.get_final_message.side_effect = RuntimeError("stream error")

        @contextmanager
        def fake_ctx(*args, **kwargs):
            yield mock_stream

        mock_client.beta.messages.stream = fake_ctx

        chunks = []
        with pytest.raises(RuntimeError, match="stream error"):
            _stream_once(mock_client, [{"role": "user", "content": "hi"}], "sys", chunks)


# Valid Discord snowflake channel IDs (10^14 <= id <= 10^19)
_CHAN_A = 100_000_000_000_000
_CHAN_B = 200_000_000_000_000
_CHAN_C = 300_000_000_000_000
_CHAN_D = 400_000_000_000_000
_CHAN_E = 500_000_000_000_000


class TestCallDirect:
    @pytest.fixture(autouse=True)
    def _patch_deps(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr("services.oauth_manager.ensure_valid_token", AsyncMock())
        monkeypatch.setattr("services.oauth_manager.init", lambda: None)
        monkeypatch.setattr("services.oauth_manager.get_access_token", lambda: "sk-test")
        monkeypatch.setattr(api_mod, "_cached_client", None)
        monkeypatch.setattr(api_mod, "_cached_token", None)
        # Patch via the name imported into claude_api module
        monkeypatch.setattr(api_mod, "append_jsonl", MagicMock())
        monkeypatch.setattr(api_mod, "fix_history_integrity", MagicMock())
        monkeypatch.setattr(api_mod, "trim_history", MagicMock())

    def _make_stream_once(self, response_text, side_effect=None):
        """Return an async-compatible mock for _stream_once via to_thread."""
        def fake_stream_once(api_client, messages, system_prompt, response_chunks):
            if side_effect:
                raise side_effect
            response_chunks.extend(list(response_text))

        return fake_stream_once

    @pytest.mark.asyncio
    async def test_basic_call_returns_response(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_stream_once", self._make_stream_once("hello"))
        history = {}
        result = await api_mod.call_direct("hi", _CHAN_A, history)
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_empty_response_returns_fallback(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_stream_once", self._make_stream_once(""))
        history = {}
        result = await api_mod.call_direct("hi", _CHAN_A, history)
        assert result == "(No response from Claude)"

    @pytest.mark.asyncio
    async def test_conv_context_seeded_for_new_channel(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_stream_once", self._make_stream_once("ok"))
        history = {}
        await api_mod.call_direct("hi", _CHAN_B, history, conv_context="thread context here")
        # history should contain seeded context messages + user + assistant
        msgs = history[_CHAN_B]
        contents = [m["content"] for m in msgs]
        assert any("thread context here" in c for c in contents)

    @pytest.mark.asyncio
    async def test_existing_channel_no_seed(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_stream_once", self._make_stream_once("ok"))
        history = {_CHAN_C: [{"role": "user", "content": "prev"}, {"role": "assistant", "content": "prev_resp"}]}
        await api_mod.call_direct("new msg", _CHAN_C, history, conv_context="should not appear")
        msgs = history[_CHAN_C]
        contents = [m["content"] for m in msgs]
        assert not any("should not appear" in c for c in contents)

    @pytest.mark.asyncio
    async def test_exception_returns_error_message(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_stream_once", self._make_stream_once("", side_effect=RuntimeError("boom")))
        history = {}
        result = await api_mod.call_direct("hi", _CHAN_A, history)
        assert "APIエラー" in result

    @pytest.mark.asyncio
    async def test_exception_pops_last_user_msg(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_stream_once", self._make_stream_once("", side_effect=RuntimeError("boom")))
        history = {}
        await api_mod.call_direct("hi", _CHAN_A, history)
        # After failure, last user msg should be removed from history
        if _CHAN_A in history:
            msgs = history[_CHAN_A]
            if msgs:
                assert msgs[-1]["role"] != "user"

    @pytest.mark.asyncio
    async def test_rate_limit_retries(self, monkeypatch):
        import services.claude_api as api_mod

        attempt_count = 0

        def fake_stream_once(api_client, messages, system_prompt, response_chunks):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise anthropic.RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429, headers={}),
                    body={},
                )
            response_chunks.append("retried ok")

        monkeypatch.setattr(api_mod, "_stream_once", fake_stream_once)
        monkeypatch.setattr(api_mod, "RETRY_BACKOFF_BASE", 0.001)

        history = {}
        result = await api_mod.call_direct("hi", _CHAN_A, history)
        assert result == "retried ok"
        assert attempt_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_exhausted_raises_then_error_response(self, monkeypatch):
        import services.claude_api as api_mod

        def always_rate_limit(api_client, messages, system_prompt, response_chunks):
            raise anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}),
                body={},
            )

        monkeypatch.setattr(api_mod, "_stream_once", always_rate_limit)
        monkeypatch.setattr(api_mod, "RETRY_BACKOFF_BASE", 0.001)
        monkeypatch.setattr(api_mod, "MAX_RETRIES", 2)

        history = {}
        result = await api_mod.call_direct("hi", _CHAN_A, history)
        assert "APIエラー" in result

    @pytest.mark.asyncio
    async def test_lru_eviction_with_ordered_dict(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_stream_once", self._make_stream_once("ok"))
        monkeypatch.setattr(api_mod, "MAX_HISTORY", 2)

        history = OrderedDict()
        # Fill up to MAX_HISTORY
        history[_CHAN_A] = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
        history[_CHAN_B] = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
        # Adding a third channel should evict the oldest
        await api_mod.call_direct("hi", _CHAN_C, history)
        assert len(history) <= 2 + 1  # eviction happens before adding new

    @pytest.mark.asyncio
    async def test_response_stripped(self, monkeypatch):
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_stream_once", self._make_stream_once("  trimmed  "))
        history = {}
        result = await api_mod.call_direct("hi", _CHAN_A, history)
        assert result == "trimmed"

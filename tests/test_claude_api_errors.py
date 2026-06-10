"""Tests for services.claude_api.errors — classification + redaction."""

from unittest.mock import MagicMock

import anthropic
import pytest

from services.claude_api.errors import (
    NON_PERSISTABLE_REPLIES,
    _classify_error,
    _error_message,
    _is_transient,
    _redact_secrets,
    classify_anthropic_error,
    is_non_persistable_reply,
)


class TestClassifyAnthropicError:
    def _make_status_error(self, status: int, body: dict | None = None):
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = status
        exc.body = body or {}
        exc.response = MagicMock()
        exc.request = MagicMock()
        return exc

    def test_rate_limit(self):
        exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        assert classify_anthropic_error(exc) == "rate_limit"

    def test_overloaded_529(self):
        assert classify_anthropic_error(self._make_status_error(529)) == "overloaded"

    def test_api_connection_error_is_overloaded(self):
        exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
        assert classify_anthropic_error(exc) == "overloaded"

    def test_auth_401(self):
        exc = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
        assert classify_anthropic_error(exc) == "auth"

    def test_permission_denied(self):
        exc = anthropic.PermissionDeniedError.__new__(anthropic.PermissionDeniedError)
        assert classify_anthropic_error(exc) == "auth"

    def test_context_overflow_via_bad_request(self):
        exc = self._make_status_error(
            400,
            body={"error": {"message": "input is too long for the model"}},
        )
        assert classify_anthropic_error(exc) == "context_overflow"

    def test_generic_500_is_overloaded(self):
        # 5xx are now transient (retried once). Previously misclassified api_exit.
        exc = self._make_status_error(500, body={"error": {"message": "internal error"}})
        assert classify_anthropic_error(exc) == "overloaded"

    def test_stream_overloaded_200(self):
        # mid-stream overloaded_error rides an HTTP-200 connection; classify
        # on body.error.type, not the (misleading) 200 status.
        exc = self._make_status_error(
            200,
            body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        )
        assert classify_anthropic_error(exc) == "overloaded"

    def test_stream_rate_limit_200(self):
        exc = self._make_status_error(200, body={"error": {"type": "rate_limit_error"}})
        assert classify_anthropic_error(exc) == "rate_limit"

    def test_invalid_request_400_type(self):
        exc = self._make_status_error(
            400,
            body={"error": {"type": "invalid_request_error", "message": "bad request"}},
        )
        assert classify_anthropic_error(exc) == "invalid_request"

    def test_context_overflow_200(self):
        # context-overflow must win over invalid_request even on a 200 stream.
        exc = self._make_status_error(
            200,
            body={"error": {"message": "input is too long for the model"}},
        )
        assert classify_anthropic_error(exc) == "context_overflow"

    def test_api_error_type_is_transient(self):
        exc = self._make_status_error(200, body={"error": {"type": "api_error"}})
        assert classify_anthropic_error(exc) == "overloaded"

    def test_empty_body_200_is_api_exit(self):
        # Unclassifiable (200 + empty body) → api_exit fallback preserved.
        assert classify_anthropic_error(self._make_status_error(200, body={})) == "api_exit"

    def test_unknown_exception(self):
        assert classify_anthropic_error(RuntimeError("???")) == "unknown"


class TestClassifyErrorString:
    @pytest.mark.parametrize("text,want", [
        ("Rate_limit hit", "rate_limit"),
        ("HTTP 429 Too Many Requests", "rate_limit"),
        ("overloaded_error", "overloaded"),
        ("authentication failed", "auth"),
        ("input is too long", "context_overflow"),
        ("max_turns reached", "max_turns"),
        ("CLI exit code 1", "api_exit"),
        ("nothing matches here", "unknown"),
    ])
    def test_patterns(self, text, want):
        assert _classify_error(text, "") == want


class TestErrorMessage:
    def test_known_types(self):
        for t in ("rate_limit", "overloaded", "auth", "context_overflow",
                  "max_turns", "api_exit"):
            assert _error_message(t).strip()

    def test_legacy_cli_exit_alias(self):
        # Backwards-compat: callers still using the old SDK-era key.
        assert _error_message("cli_exit") == _error_message("api_exit")

    def test_unknown_fallback(self):
        assert "❌" in _error_message("brand_new_category")

    def test_overloaded_is_not_the_unknown_fallback(self):
        # ADR-0006: an overloaded retry-exhaustion must surface the overloaded
        # message, never the generic "内部エラー" fallback. If these ever
        # collapse to the same string, the user can no longer tell a transient
        # server overload from a real internal error.
        overloaded = _error_message("overloaded")
        fallback = _error_message("brand_new_category")
        assert overloaded != fallback
        assert "内部エラー" not in overloaded
        assert "混雑" in overloaded


class TestNonPersistableReply:
    """ADR-0008: sentinel/error replies must never enter the JSONL history,
    or they prime fable-5 into a self-reinforcing refusal loop."""

    def test_no_response_sentinel_blocked(self):
        assert is_non_persistable_reply("(no response)")

    def test_all_error_messages_blocked(self):
        # Every user-facing error string the bot can emit must be non-persistable.
        for et in ("rate_limit", "overloaded", "auth", "context_overflow",
                   "max_turns", "invalid_request", "api_exit"):
            assert is_non_persistable_reply(_error_message(et)), et
        assert is_non_persistable_reply(_error_message("brand_new"))  # fallback
        assert is_non_persistable_reply("⏰ タイムアウトしました")

    def test_real_reply_is_persistable(self):
        assert not is_non_persistable_reply("先輩、それは違いますよ。")
        # A real reply that merely mentions an error word is still persisted.
        assert not is_non_persistable_reply("さっき (no response) って出てましたね")

    def test_whitespace_stripped(self):
        assert is_non_persistable_reply("  (no response)\n")

    def test_empty_is_not_flagged_here(self):
        # Empty string is handled by the separate `if response_text:` guard,
        # not this set — keep them distinct.
        assert not is_non_persistable_reply("")
        assert not is_non_persistable_reply("   ")

    def test_set_covers_no_response_and_errors(self):
        assert "(no response)" in NON_PERSISTABLE_REPLIES
        assert len(NON_PERSISTABLE_REPLIES) >= 8


class TestIsTransient:
    @pytest.mark.parametrize("t,expected", [
        ("rate_limit", True),
        ("overloaded", True),
        ("auth", False),
        ("context_overflow", False),
        ("max_turns", False),
        ("api_exit", False),
        ("unknown", False),
    ])
    def test_buckets(self, t, expected):
        assert _is_transient(t) is expected


class TestRedactSecrets:
    def test_redacts_anthropic_env_var(self):
        out = _redact_secrets("ANTHROPIC_API_KEY=sk-ant-abc123\nrest")
        assert "sk-ant-abc123" not in out
        assert "ANTHROPIC_API_KEY=***" in out

    def test_redacts_bearer_token(self):
        out = _redact_secrets("Bearer sk-ant-deadbeef_token-x")
        assert "sk-ant-deadbeef_token-x" not in out
        assert "sk-ant-***" in out

    def test_redacts_auth_token_env(self):
        out = _redact_secrets("ANTHROPIC_AUTH_TOKEN=eyJhbGc")
        assert "eyJhbGc" not in out

    def test_no_secrets_unchanged(self):
        plain = "ordinary log line with no key"
        assert _redact_secrets(plain) == plain

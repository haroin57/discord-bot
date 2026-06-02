"""Tests for services.claude_api.errors — classification + redaction."""

from unittest.mock import MagicMock

import anthropic
import pytest

from services.claude_api.errors import (
    _classify_error,
    _error_message,
    _is_transient,
    _redact_secrets,
    classify_anthropic_error,
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

    def test_413_request_too_large_is_context_overflow(self):
        # 413 (oversized resumed history) must self-heal via the context_overflow
        # path — drop the session, resume from a short tail — instead of the old
        # api_exit dead-end that re-sent the same payload forever (msg2 incident).
        exc = self._make_status_error(
            413,
            body={"error": {"type": "request_too_large",
                            "message": "Request exceeds the maximum size"}},
        )
        assert classify_anthropic_error(exc) == "context_overflow"

    def test_request_too_large_type_on_200_is_context_overflow(self):
        # The same error can ride a 200 stream with only the body.error.type set.
        exc = self._make_status_error(
            200,
            body={"error": {"type": "request_too_large", "message": "too big"}},
        )
        assert classify_anthropic_error(exc) == "context_overflow"

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

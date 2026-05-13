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

    def test_generic_500_is_api_exit(self):
        exc = self._make_status_error(500, body={"error": {"message": "internal error"}})
        assert classify_anthropic_error(exc) == "api_exit"

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

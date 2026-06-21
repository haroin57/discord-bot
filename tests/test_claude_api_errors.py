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
    coerce_tool_input,
    contains_text_tool_call,
    is_non_persistable_reply,
    parse_text_tool_calls,
    resolve_tool_name,
    strip_degraded_opener_token,
    strip_text_tool_call,
)


class TestTextToolCallPoison:
    """Detect/strip tool calls emitted as plain TEXT (2026-06-13 poison)."""

    POISON = (
        "初値、確認します。お父さん少々お待ちを。\n\ncourse\n"
        '<invoke name="brave_news_search">\n'
        '<parameter name="query">SpaceX SPCX</parameter>\n'
        '<parameter name="count">10</parameter>'
    )

    def test_detects_invoke_with_parameter(self):
        assert contains_text_tool_call(self.POISON) is True

    def test_detects_function_calls_block(self):
        s = '承知。\n<function_calls>\n<invoke name="Bash">\n</invoke>\n</function_calls>'
        assert contains_text_tool_call(s) is True

    def test_strip_keeps_natural_language_prefix(self):
        assert strip_text_tool_call(self.POISON) == (
            "初値、確認します。お父さん少々お待ちを。"
        )
        # the degraded "course" opener token is removed too
        assert "course" not in strip_text_tool_call(self.POISON)
        assert "<invoke" not in strip_text_tool_call(self.POISON)

    def test_clean_text_untouched(self):
        clean = "普通の返事です。ツールは使いません。"
        assert contains_text_tool_call(clean) is False
        assert strip_text_tool_call(clean) == clean

    def test_bare_mention_not_flagged(self):
        # A passing mention without a parameter / closing tag must not trip
        # (avoid stripping prose that merely discusses the syntax).
        s = '`<invoke name="X">` のような書き方はテキストでは無効です'
        assert contains_text_tool_call(s) is False

    def test_empty_safe(self):
        assert contains_text_tool_call("") is False
        assert strip_text_tool_call("") == ""

    def test_strips_court_degraded_token(self):
        # `court` is a sibling of the `course` corrupt <function_calls> token.
        s = ('やります。\ncourt\n<invoke name="Bash">\n'
             '<parameter name="command">ls</parameter>\n</invoke>')
        out = strip_text_tool_call(s)
        assert out == "やります。"
        assert "court" not in out

    def test_court_in_prose_without_xml_untouched(self):
        # The word alone (no following XML opener) must not be touched — the
        # structural detector gates the whole strip.
        clean = "The tennis court was booked all afternoon."
        assert contains_text_tool_call(clean) is False
        assert strip_text_tool_call(clean) == clean


class TestStripDegradedOpenerToken:
    """Strip a degraded <function_calls>/<invoke> opener token (course/court/
    sit/…) left BARE on its own line while a REAL tool_use block also fires.

    No following <invoke> XML, so strip_text_tool_call can't catch it; the bare
    token would otherwise leak into Discord AND the transcript (self-poison
    seed). Matched structurally — known token alone on a line — so prose is
    never touched.
    """

    def test_strips_bare_court_on_own_line(self):
        s = "適用しました。\n\ncourt\n\n3箇所とも成功"
        out = strip_degraded_opener_token(s)
        assert "court" not in out
        assert out == "適用しました。\n\n3箇所とも成功"

    def test_strips_bare_sit_on_own_line(self):
        # `sit` is the regression token that slipped past the hardcoded list.
        s = "再起動します。\n\nsit\n\n完了"
        assert strip_degraded_opener_token(s) == "再起動します。\n\n完了"

    def test_strips_trailing_bare_token(self):
        s = "確認します。\n\ncourt"
        assert strip_degraded_opener_token(s) == "確認します。"

    def test_strips_function_calls_token(self):
        s = "やります。\n\nfunction_calls\n\nここから"
        assert strip_degraded_opener_token(s) == "やります。\n\nここから"

    def test_prose_court_backtick_untouched(self):
        clean = "さっきの `court` 連発、よく分かった"
        assert strip_degraded_opener_token(clean) == clean

    def test_court_mid_sentence_untouched(self):
        clean = "court が何なのか確認します"
        assert strip_degraded_opener_token(clean) == clean

    def test_partial_match_sit_untouched(self):
        # `sit` inside `visit` must not be touched (line-boundary anchored).
        clean = "I will visit the site soon"
        assert strip_degraded_opener_token(clean) == clean

    def test_normal_paragraph_untouched(self):
        clean = "まず確認します。\n\n次に実装します"
        assert strip_degraded_opener_token(clean) == clean

    def test_empty_safe(self):
        assert strip_degraded_opener_token("") == ""
        assert strip_degraded_opener_token("text") == "text"


class TestParseTextToolCalls:
    def test_single_invoke_one_param(self):
        s = ('course\n<invoke name="brave_news_search">\n'
             '<parameter name="query">SpaceX</parameter>\n</invoke>')
        assert parse_text_tool_calls(s) == [
            {"name": "brave_news_search", "input": {"query": "SpaceX"}},
        ]

    def test_function_calls_wrapper(self):
        s = ('<function_calls>\n<invoke name="Bash">\n'
             '<parameter name="command">ls -la</parameter>\n</invoke>\n'
             '</function_calls>')
        assert parse_text_tool_calls(s) == [
            {"name": "Bash", "input": {"command": "ls -la"}},
        ]

    def test_antml_namespace(self):
        # Build the namespaced tags at runtime so this source file never
        # contains the harness's real tool-call delimiters verbatim.
        ns = "antml:"
        s = (f"<{ns}invoke name=\"Read\">\n"
             f"<{ns}parameter name=\"file_path\">/x</{ns}parameter>\n"
             f"</{ns}invoke>")
        assert parse_text_tool_calls(s) == [
            {"name": "Read", "input": {"file_path": "/x"}},
        ]

    def test_multiple_invokes_in_order(self):
        s = ('<invoke name="A">\n<parameter name="p">1</parameter>\n</invoke>\n'
             '<invoke name="B">\n<parameter name="q">2</parameter>\n</invoke>')
        assert parse_text_tool_calls(s) == [
            {"name": "A", "input": {"p": "1"}},
            {"name": "B", "input": {"q": "2"}},
        ]

    def test_value_whitespace_preserved(self):
        s = ('<invoke name="Bash">\n'
             '<parameter name="command">  indented\n  line2</parameter>\n</invoke>')
        assert parse_text_tool_calls(s) == [
            {"name": "Bash", "input": {"command": "  indented\n  line2"}},
        ]

    def test_empty_param(self):
        s = '<invoke name="X">\n<parameter name="p"></parameter>\n</invoke>'
        assert parse_text_tool_calls(s) == [{"name": "X", "input": {"p": ""}}]

    def test_truncated_trailing_invoke(self):
        # No closing </invoke> but a complete <parameter> → still parses.
        s = '<invoke name="brave_news_search">\n<parameter name="query">x</parameter>'
        assert parse_text_tool_calls(s) == [
            {"name": "brave_news_search", "input": {"query": "x"}},
        ]

    def test_non_invoke_prose_empty(self):
        assert parse_text_tool_calls("普通の文章です") == []
        assert parse_text_tool_calls("") == []


class TestResolveToolName:
    def test_exact_match(self):
        assert resolve_tool_name("Read", ["Read", "Bash"]) == "Read"

    def test_suffix_match(self):
        assert resolve_tool_name(
            "brave_news_search", ["mcp__brave-search__brave_news_search"],
        ) == "mcp__brave-search__brave_news_search"

    def test_ambiguous_returns_none(self):
        assert resolve_tool_name(
            "search", ["mcp__a__search", "mcp__b__search"],
        ) is None

    def test_no_match_returns_none(self):
        assert resolve_tool_name("nope", ["Read", "Bash"]) is None

    def test_empty_emitted_returns_none(self):
        assert resolve_tool_name("", ["Read"]) is None


class TestCoerceToolInput:
    def test_integer_coercion(self):
        out = coerce_tool_input(
            {"n": "5"},
            {"type": "object", "properties": {"n": {"type": "integer"}}},
        )
        assert out == {"n": 5} and isinstance(out["n"], int)

    def test_number_coercion(self):
        out = coerce_tool_input(
            {"x": "1.5"},
            {"type": "object", "properties": {"x": {"type": "number"}}},
        )
        assert out == {"x": 1.5}

    def test_boolean_coercion(self):
        schema = {"type": "object", "properties": {"b": {"type": "boolean"}}}
        assert coerce_tool_input({"b": "true"}, schema) == {"b": True}
        assert coerce_tool_input({"b": "0"}, schema) == {"b": False}

    def test_object_coercion(self):
        out = coerce_tool_input(
            {"o": '{"a": 1}'},
            {"type": "object", "properties": {"o": {"type": "object"}}},
        )
        assert out == {"o": {"a": 1}}

    def test_failure_keeps_raw_string(self):
        out = coerce_tool_input(
            {"n": "not-an-int"},
            {"type": "object", "properties": {"n": {"type": "integer"}}},
        )
        assert out == {"n": "not-an-int"}

    def test_missing_schema_passthrough(self):
        assert coerce_tool_input({"q": "x"}, None) == {"q": "x"}

    def test_unknown_property_passthrough(self):
        out = coerce_tool_input(
            {"z": "x"},
            {"type": "object", "properties": {"n": {"type": "integer"}}},
        )
        assert out == {"z": "x"}

    def test_empty_input(self):
        assert coerce_tool_input({}, None) == {}


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


class TestBillingClassification:
    """billing カテゴリ: クレジット枯渇は rate_limit でなく billing に再分類される。"""

    def _make_status_error(self, status: int, body: dict | None = None, headers: dict | None = None):
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = status
        exc.body = body or {}
        mock_resp = MagicMock()
        mock_resp.headers = headers or {}
        exc.response = mock_resp
        exc.request = MagicMock()
        return exc

    def test_credit_429_is_billing_not_rate_limit(self):
        """rate_limit_error type + credit message → billing (not rate_limit)."""
        exc = self._make_status_error(
            429,
            body={"error": {
                "type": "rate_limit_error",
                "message": "Usage credits are required for long context requests",
            }},
        )
        assert classify_anthropic_error(exc) == "billing"

    def test_billing_is_not_transient(self):
        exc = self._make_status_error(
            429,
            body={"error": {
                "type": "rate_limit_error",
                "message": "Usage credits are required for long context requests",
            }},
        )
        assert _is_transient(classify_anthropic_error(exc)) is False

    def test_billing_has_message(self):
        from services.claude_api.errors import _error_message
        msg = _error_message("billing")
        assert "クレジット" in msg or "credit" in msg.lower()
        assert "リトライ" in msg

    def test_credit_balance_substring(self):
        exc = self._make_status_error(
            429,
            body={"error": {"type": "rate_limit_error", "message": "Insufficient credit balance"}},
        )
        assert classify_anthropic_error(exc) == "billing"

    def test_upgrade_substring(self):
        exc = self._make_status_error(
            429,
            body={"error": {"type": "rate_limit_error", "message": "Please upgrade your plan"}},
        )
        assert classify_anthropic_error(exc) == "billing"

    def test_x_should_retry_false_on_429_is_billing(self):
        """x-should-retry: false on a 429 → billing (server says don't retry)."""
        exc = self._make_status_error(
            429,
            body={"error": {"type": "rate_limit_error", "message": "rate limited"}},
            headers={"x-should-retry": "false"},
        )
        assert classify_anthropic_error(exc) == "billing"

    def test_x_should_retry_true_on_429_stays_rate_limit(self):
        """x-should-retry: true + no credit keyword → normal rate_limit."""
        exc = self._make_status_error(
            429,
            body={"error": {"type": "rate_limit_error", "message": "rate limited"}},
            headers={"x-should-retry": "true"},
        )
        assert classify_anthropic_error(exc) == "rate_limit"

    def test_rate_limit_error_instance_with_credit_body(self):
        """anthropic.RateLimitError instance carrying credit message → billing."""
        exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        # body attribute won't exist on a naked instance, but _extract_fields
        # handles that gracefully (returns empty string).
        # Give it a body to simulate the real SDK path.
        exc.status_code = 429
        exc.body = {"error": {
            "type": "rate_limit_error",
            "message": "Usage credits are required for long context requests",
        }}
        mock_resp = MagicMock()
        mock_resp.headers = {}
        exc.response = mock_resp
        exc.request = MagicMock()
        assert classify_anthropic_error(exc) == "billing"

    def test_ordinary_rate_limit_error_instance_stays(self):
        """anthropic.RateLimitError without credit body → rate_limit (unchanged)."""
        exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        exc.status_code = 429
        exc.body = {"error": {"type": "rate_limit_error", "message": "Too many requests"}}
        mock_resp = MagicMock()
        mock_resp.headers = {}
        exc.response = mock_resp
        exc.request = MagicMock()
        assert classify_anthropic_error(exc) == "rate_limit"

    def test_billing_in_non_persistable_replies(self):
        from services.claude_api.errors import _error_message, NON_PERSISTABLE_REPLIES
        assert _error_message("billing") in NON_PERSISTABLE_REPLIES


class TestAuditClassifications:
    """2026-06-13 CLI audit: model_not_found (#6) / tool_use_mismatch (#14)."""

    def _make_status_error(self, status: int, body: dict | None = None):
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = status
        exc.body = body or {}
        exc.response = MagicMock()
        exc.request = MagicMock()
        return exc

    def test_not_found_error_instance(self):
        exc = anthropic.NotFoundError.__new__(anthropic.NotFoundError)
        assert classify_anthropic_error(exc) == "model_not_found"

    def test_404_status_is_model_not_found(self):
        assert classify_anthropic_error(
            self._make_status_error(404)
        ) == "model_not_found"

    def test_not_found_error_type_in_body(self):
        exc = self._make_status_error(
            400, {"error": {"type": "not_found_error", "message": "no model"}}
        )
        assert classify_anthropic_error(exc) == "model_not_found"

    @pytest.mark.parametrize("msg", [
        "messages.5: tool_use ids were found without tool_result blocks "
        "immediately after",
        "unexpected `tool_use_id` found in tool_result blocks",
        "tool_result block(s) provided without corresponding tool_use",
        "each tool_result must have a corresponding tool_use in the previous "
        "message",
    ])
    def test_pairing_400_is_tool_use_mismatch(self, msg):
        exc = self._make_status_error(
            400, {"error": {"type": "invalid_request_error", "message": msg}}
        )
        assert classify_anthropic_error(exc) == "tool_use_mismatch"

    def test_plain_400_stays_invalid_request(self):
        exc = self._make_status_error(
            400, {"error": {"type": "invalid_request_error",
                            "message": "messages: field required"}}
        )
        assert classify_anthropic_error(exc) == "invalid_request"

    def test_new_categories_are_not_transient(self):
        assert _is_transient("model_not_found") is False
        assert _is_transient("tool_use_mismatch") is False

    def test_new_categories_have_messages(self):
        assert "モデル" in _error_message("model_not_found")
        assert "リセット" in _error_message("tool_use_mismatch")

"""Error classification + redaction for direct Anthropic API path.

Two classifiers exist:

* ``classify_anthropic_error(exc)`` — primary: maps anthropic SDK exceptions
  to one of ``rate_limit / overloaded / auth / context_overflow / api_exit /
  unknown``. Used by the new tool_loop on every API call.
* ``_classify_error(err_str, stderr_tail)`` — legacy fallback for log-tail
  strings. Kept (under the same name as in services/claude_sdk.py) so error
  paths that only have a captured string can still be categorized.

Ported from services/claude_sdk.py:122-219. ``cli_exit`` was renamed
``api_exit`` for the direct-API world; ``_error_message`` aliases the old
key for backwards compat.
"""

import re

import anthropic

# ---------------------------------------------------------------------------
# String patterns (legacy fallback)
# ---------------------------------------------------------------------------
_RATE_LIMIT_PATTERNS = (
    "rate_limit", "rate-limit", "429", "too many requests", "ratelimiterror",
)
_OVERLOAD_PATTERNS = ("overloaded", "529", "server_overload")
_AUTH_PATTERNS = (
    "authentication failed", "401 unauthorized", "403 forbidden",
    "invalid api key", "token revoked", "not logged in",
)
_CONTEXT_PATTERNS = (
    "too long", "token limit", "exceed context limit", "max_tokens exceed",
)
_MAX_TURNS_PATTERNS = ("max_turns", "max turns", "turn limit")


def _classify_error(err_str: str, stderr_tail: str = "") -> str:
    """Legacy string-based classifier. Used when only a log tail is available."""
    combined = f"{err_str}\n{stderr_tail}".lower()
    if any(p in combined for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if any(p in combined for p in _OVERLOAD_PATTERNS):
        return "overloaded"
    if any(p in combined for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in combined for p in _CONTEXT_PATTERNS):
        return "context_overflow"
    if any(p in combined for p in _MAX_TURNS_PATTERNS):
        return "max_turns"
    if "exit code 1" in combined:
        return "api_exit"  # was "cli_exit" in claude_sdk
    return "unknown"


def classify_anthropic_error(exc: BaseException) -> str:
    """Primary classifier — maps anthropic SDK exceptions to retry categories."""
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limit"
    if isinstance(exc, anthropic.AuthenticationError):
        return "auth"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "auth"
    if isinstance(exc, anthropic.APIConnectionError):
        return "overloaded"
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        err_type = ""
        body_text = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                err_type = str(err.get("type", "")).lower()
                body_text = str(err.get("message", ""))
            else:
                body_text = str(err or "")
        else:
            body_text = str(body or "")
        body_lower = body_text.lower()
        # Stream-internal error events ride the HTTP-200 connection, so the
        # SDK raises a bare APIStatusError(status_code=200) and the true kind
        # lives in body.error.type. Classify on error.type, not status alone:
        # a 200 + overloaded_error previously fell through to api_exit and was
        # never retried (the bug this fixes).
        if status == 529 or "overloaded" in err_type or "overloaded" in body_lower:
            return "overloaded"
        if status == 429 or "rate_limit" in err_type:
            return "rate_limit"
        # 413 request_too_large: the payload (huge resumed history) exceeds the
        # request size limit. The body — "Request exceeds the maximum size" /
        # "request_too_large" — matches NONE of _CONTEXT_PATTERNS, so before
        # 2026-06-01 this fell through to api_exit → non-transient → entry.py
        # returned immediately WITHOUT dropping the session, so the next message
        # re-sent the same oversized history and 413'd again (msg2 incident).
        # Route it to context_overflow so entry.py's handler drops the session
        # and the next turn resumes from a short JSONL tail (self-heal).
        if (
            status == 413
            or "request_too_large" in err_type
            or "exceeds the maximum size" in body_lower
            or "request too large" in body_lower
        ):
            return "context_overflow"
        # context-overflow must be checked BEFORE invalid_request so a 400 with
        # an "input too long" body stays context_overflow (existing behaviour).
        if any(p in body_lower for p in _CONTEXT_PATTERNS):
            return "context_overflow"
        # 400 Bad Request — deterministic; retrying won't help.
        if status == 400 or err_type == "invalid_request_error":
            return "invalid_request"
        # Generic 5xx / api_error are transient server hiccups — retry once.
        # _is_transient bounds attempts via TRANSIENT_MAX_ATTEMPTS, so this
        # can't spin forever.
        if (status is not None and status >= 500) or err_type == "api_error":
            return "overloaded"
        return "api_exit"
    return "unknown"


def _error_message(error_type: str) -> str:
    """User-facing message for a classified error type."""
    table = {
        "rate_limit": "⏳ APIのレート制限に達しました。少し待ってから再試行してください。",
        "overloaded": "🔄 サーバーが混雑しています。しばらく待ってから再試行してください。",
        "auth": "🔑 認証エラーが発生しました。管理者に連絡してください。",
        "context_overflow": "📝 メッセージが長すぎます。短くして送り直してください。",
        "max_turns": "🔄 ターン上限に達しました。もう一度話しかけてください。",
        "invalid_request": "⚠️ リクエストの形式に問題があり処理できませんでした。もう一度試しても直らない場合は管理者に連絡してください。",
        "api_exit": "⚠️ 処理が中断されました。もう一度試してください。",
        # Backwards-compat alias for callers still using the SDK-era key.
        "cli_exit": "⚠️ 処理が中断されました。もう一度試してください。",
    }
    return table.get(error_type, "❌ 内部エラーが発生しました。再試行してください。")


def _is_transient(error_type: str) -> bool:
    return error_type in ("rate_limit", "overloaded")


def _redact_secrets(text: str) -> str:
    """Remove API keys / auth tokens from text before writing to disk or logs."""
    text = re.sub(r"(ANTHROPIC_(?:API|AUTH)_(?:KEY|TOKEN))=\S+", r"\1=***", text)
    text = re.sub(r"sk-ant-[A-Za-z0-9_-]+", "sk-ant-***", text)
    return text

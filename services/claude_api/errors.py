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
        if status == 529:
            return "overloaded"
        # BadRequestError is a subclass; check body for context-overflow signal
        body = getattr(exc, "body", None)
        body_text = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                body_text = str(err.get("message", ""))
            else:
                body_text = str(err or "")
        else:
            body_text = str(body or "")
        body_lower = body_text.lower()
        if any(p in body_lower for p in _CONTEXT_PATTERNS):
            return "context_overflow"
        # Other 4xx / 5xx
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

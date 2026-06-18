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


# The Messages API's tool_use/tool_result pairing 400s (audit #14). These are
# recoverable — the replayed transcript desynced, not the user's request — so
# entry.py drops the session and the next turn reloads a clean (Yy8-swept)
# window instead of treating the channel as permanently broken.
_TOOL_USE_MISMATCH_PATTERNS = (
    "tool_use ids were found without tool_result",
    "tool_result block(s) provided without",
    "unexpected `tool_use_id`",
    "unexpected tool_use_id",
    "must have a corresponding tool_use",
)


def classify_anthropic_error(exc: BaseException) -> str:
    """Primary classifier — maps anthropic SDK exceptions to retry categories."""
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limit"
    if isinstance(exc, anthropic.NotFoundError):
        # Model renamed/retired (audit #6) — entry.py escapes to the fallback
        # model once instead of surfacing a permanent error.
        return "model_not_found"
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
        # context-overflow must be checked BEFORE invalid_request so a 400 with
        # an "input too long" body stays context_overflow (existing behaviour).
        if any(p in body_lower for p in _CONTEXT_PATTERNS):
            return "context_overflow"
        if status == 404 or err_type == "not_found_error":
            return "model_not_found"
        # tool_use/tool_result pairing 400 — recoverable via session drop
        # (audit #14); must be checked BEFORE the generic invalid_request.
        if any(p in body_lower for p in _TOOL_USE_MISMATCH_PATTERNS):
            return "tool_use_mismatch"
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


_ERROR_MESSAGE_TABLE = {
    "rate_limit": "⏳ APIのレート制限に達しました。少し待ってから再試行してください。",
    "overloaded": "🔄 サーバーが混雑しています。しばらく待ってから再試行してください。",
    "auth": "🔑 認証エラーが発生しました。管理者に連絡してください。",
    "context_overflow": "📝 メッセージが長すぎます。短くして送り直してください。",
    "max_turns": "🔄 ターン上限に達しました。もう一度話しかけてください。",
    "invalid_request": "⚠️ リクエストの形式に問題があり処理できませんでした。もう一度試しても直らない場合は管理者に連絡してください。",
    "model_not_found": "⚠️ モデルが見つかりませんでした。管理者に連絡してください。",
    "tool_use_mismatch": "🔁 会話の内部状態が壊れていたためリセットしました。もう一度送ってください。",
    "api_exit": "⚠️ 処理が中断されました。もう一度試してください。",
    # Backwards-compat alias for callers still using the SDK-era key.
    "cli_exit": "⚠️ 処理が中断されました。もう一度試してください。",
}
_ERROR_MESSAGE_FALLBACK = "❌ 内部エラーが発生しました。再試行してください。"

# Sentinel/error replies that must NEVER be persisted to the JSONL history.
# Replaying these back into a resumed transcript primes the model (especially
# fable-5) to keep emitting empty/refused turns — a self-reinforcing poison
# loop that surfaces as recurring "(no response)" (ADR-0008). These are exact
# strings the bot emits when no real model text was produced; persisting them
# adds no conversational signal and actively degrades resume quality.
NON_PERSISTABLE_REPLIES: frozenset[str] = frozenset(
    {"(no response)", "⏰ タイムアウトしました", _ERROR_MESSAGE_FALLBACK}
    | set(_ERROR_MESSAGE_TABLE.values())
)


def is_non_persistable_reply(text: str) -> bool:
    """True if ``text`` is a sentinel/error reply that must not enter history.

    Exact-match only — a real model reply that merely *mentions* an error
    string is still persisted. Whitespace is stripped so trailing newlines
    from the delivery layer don't defeat the check.
    """
    return (text or "").strip() in NON_PERSISTABLE_REPLIES


def _error_message(error_type: str) -> str:
    """User-facing message for a classified error type."""
    return _ERROR_MESSAGE_TABLE.get(error_type, _ERROR_MESSAGE_FALLBACK)


def _is_transient(error_type: str) -> bool:
    return error_type in ("rate_limit", "overloaded")


def _redact_secrets(text: str) -> str:
    """Remove API keys / auth tokens from text before writing to disk or logs."""
    text = re.sub(r"(ANTHROPIC_(?:API|AUTH)_(?:KEY|TOKEN))=\S+", r"\1=***", text)
    text = re.sub(r"sk-ant-[A-Za-z0-9_-]+", "sk-ant-***", text)
    return text


# ---------------------------------------------------------------------------
# Text-form tool-call ("<invoke …>") poison detection (2026-06-13)
# ---------------------------------------------------------------------------
# A model can emit a tool call as PLAIN TEXT — an ``<invoke name="…">`` /
# ``<function_calls>`` XML span inside a normal text block — instead of a real
# ``tool_use`` block. The agent loop only executes ``type=="tool_use"`` blocks,
# so such a call never runs, the raw XML is shown to the user, and (worse) it
# lands in the JSONL transcript, where the model mimics its own broken format
# on every resume — a self-poison loop of the ADR-0008 class, but for tool-call
# syntax. See docs/2026-06-13_tool_call_text_invoke_poison.md.
#
# Detection requires the *structural* pattern (an invoke with a parameter or a
# closing tag, or an explicit ``<function_calls>``), not a bare mention, to
# avoid stripping prose that merely talks about the syntax.
_TEXT_TOOL_CALL_RE = re.compile(
    r"<\s*(?:antml:)?function_calls\b"
    r"|<\s*(?:antml:)?invoke\s+name\s*=\s*\"[^\"]+\""
    r"[\s\S]*?(?:<\s*(?:antml:)?parameter\b|</\s*(?:antml:)?invoke\s*>)",
    re.IGNORECASE,
)
# Opener used to find where to cut when stripping (the earliest XML opener).
_TEXT_TOOL_CALL_OPEN_RE = re.compile(
    r"<\s*(?:antml:)?(?:function_calls\b|invoke\s+name\s*=)",
    re.IGNORECASE,
)


def contains_text_tool_call(text: str) -> bool:
    """True if ``text`` carries a tool call emitted as plain text (not tool_use)."""
    return bool(text) and _TEXT_TOOL_CALL_RE.search(text) is not None


def strip_text_tool_call(text: str) -> str:
    """Return ``text`` with a trailing text-form tool-call span removed.

    Cuts from the first XML opener to the end (once the model starts typing
    tool-call XML as text, the rest of the message is that garbage), then
    drops a dangling degraded opener token (``course`` / ``function_calls``)
    left on the preceding line. The natural-language prefix is preserved.
    """
    if not contains_text_tool_call(text):
        return text
    m = _TEXT_TOOL_CALL_OPEN_RE.search(text)
    if m is None:
        return text
    head = text[: m.start()].rstrip()
    head = re.sub(r"\b(?:course|function_calls)\s*$", "", head).rstrip()
    return head

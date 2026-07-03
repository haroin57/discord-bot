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

import json
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


_BILLING_SUBSTRINGS = (
    "usage credits are required",
    "credit balance",
    "insufficient",
    "quota",
    "upgrade",
    "long context",
    "billing",
)


# Tool Search Tool / defer_loading をこの API 経路が受け付けない場合の 400 検知
# （tool_catalog の auto-off 用）。エラーメッセージの正確な文言は経路依存なので
# substring 判定（_TOOL_USE_MISMATCH_PATTERNS と同じ流儀）。
_TOOL_SEARCH_ERROR_SUBSTRINGS = (
    "tool_search",
    "defer_loading",
    "tool_reference",
)


def is_tool_search_request_error(exc: BaseException) -> bool:
    """400 invalid_request が Tool Search / defer_loading 起因かを判定する。"""
    status, _err_type, message, _headers = _extract_fields(exc)
    if status is not None and status != 400:
        return False
    body_lower = (message or "").lower()
    return any(p in body_lower for p in _TOOL_SEARCH_ERROR_SUBSTRINGS)


def _extract_fields(exc: BaseException) -> tuple["int | None", str, str, dict]:
    """Extract (status, err_type, message, headers) from an anthropic SDK exception.

    Reads exc.body (dict with error.type / error.message) and
    exc.response.headers (lowercased).  Safe to call on any exception type —
    returns sensible defaults when attributes are absent.
    """
    status: "int | None" = getattr(exc, "status_code", None)
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

    resp = getattr(exc, "response", None)
    raw_headers = getattr(resp, "headers", None) if resp is not None else None
    headers: dict = {}
    if raw_headers is not None:
        try:
            headers = {k.lower(): v for k, v in raw_headers.items()}
        except Exception:
            pass

    return status, err_type, body_text, headers


def classify_anthropic_error(exc: BaseException) -> str:
    """Primary classifier — maps anthropic SDK exceptions to retry categories."""
    if isinstance(exc, anthropic.RateLimitError):
        # RateLimitError may still carry a credit-related message — check body.
        _, _, body_text, _ = _extract_fields(exc)
        if any(p in body_text.lower() for p in _BILLING_SUBSTRINGS):
            return "billing"
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
        status, err_type, body_text, headers = _extract_fields(exc)
        body_lower = body_text.lower()
        should_retry_hdr = headers.get("x-should-retry", "").lower()
        # Stream-internal error events ride the HTTP-200 connection, so the
        # SDK raises a bare APIStatusError(status_code=200) and the true kind
        # lives in body.error.type. Classify on error.type, not status alone:
        # a 200 + overloaded_error previously fell through to api_exit and was
        # never retried (the bug this fixes).
        if status == 529 or "overloaded" in err_type or "overloaded" in body_lower:
            return "overloaded"
        # billing: credit-related substrings win even when error.type is
        # rate_limit_error — retrying against a depleted credit pool wastes
        # API calls and the budget doesn't recover under retry pressure.
        if any(p in body_lower for p in _BILLING_SUBSTRINGS):
            return "billing"
        if status == 429 or "rate_limit" in err_type:
            # x-should-retry: false signals the server considers this permanent.
            if should_retry_hdr == "false":
                return "billing"
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
    "billing": "⚠️ APIのクレジット枠を使い切ったか、課金が必要なリクエストです（長コンテキスト等）。`/effort` で effort を下げる、枠の回復を待つ、または管理者に課金確認を依頼してください。リトライしても直りません。",
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
    # Trim a dangling degraded opener token left on the preceding line. The
    # STRUCTURAL regex above is the real detector — this list is only cosmetic
    # head-trimming and can ONLY fire on a token sitting immediately before a
    # confirmed XML opener (contains_text_tool_call already matched), so even
    # an over-broad entry here cannot strip ordinary prose. `course`/`court`
    # are the corrupt `<function_calls>` tokens observed in production logs.
    head = re.sub(
        r"\b(?:course|courses|court|sit|antml|function_calls|tool_calls)\s*$",
        "", head,
    ).rstrip()
    return head


# Degraded `<function_calls>`/`<invoke>` opener tokens (course/court/sit/…) can
# appear as a BARE token isolated on its own line while the model ALSO emits a
# genuine tool_use block. strip_text_tool_call can't catch that case (it needs
# a following <invoke> XML span), so the bare token survives into Discord AND
# the transcript — a self-poison seed (docs/2026-06-13_tool_call_text_invoke_poison).
_DEGRADED_OPENER_KNOWN = "course|courses|court|sit|antml|function_calls|tool_calls"
_DEGRADED_OPENER_LINE_RE = re.compile(
    r"(?:\A|\n)[ \t]*(?:" + _DEGRADED_OPENER_KNOWN + r")[ \t]*(?=\n|\Z)",
    re.IGNORECASE,
)


def strip_degraded_opener_token(text: str) -> str:
    """Remove a degraded tool-call opener token left isolated on its own line.

    Matches STRUCTURALLY: a known degraded token alone on a line (start- or
    newline-bounded, with line-end / string-end ahead). Prose mentions keep
    neighbours on the same line (``\u0060court\u0060`` inline, mid-sentence,
    code-fenced) and are never touched, so this is safe to run unconditionally
    on every assistant turn — independent of tool_use co-occurrence.
    """
    if not text:
        return text
    cleaned = _DEGRADED_OPENER_LINE_RE.sub("", text)
    if cleaned != text:
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# Text-form tool-call RESCUE (2026-06-21)
# ---------------------------------------------------------------------------
# When a model emits a tool call as plain text (`<invoke name="…">` with
# `<parameter>` children) instead of a real tool_use block, the call never
# runs and the turn dead-ends. Rather than only re-prompting, the tool loop
# can RESCUE the call: parse it here, resolve the (possibly abbreviated) tool
# name against the advertised set, and re-execute it as a genuine tool_use.
# Parsing is best-effort and total — it never raises; an unparseable / ambiguous
# call simply yields nothing so the caller falls back to the re-prompt path.
_INVOKE_BLOCK_RE = re.compile(
    r"<\s*(?:antml:)?invoke\s+name\s*=\s*\"(?P<name>[^\"]+)\"\s*>"
    r"(?P<body>[\s\S]*?)"
    r"(?:</\s*(?:antml:)?invoke\s*>|$)",
    re.IGNORECASE,
)
_PARAM_RE = re.compile(
    r"<\s*(?:antml:)?parameter\s+name\s*=\s*\"(?P<pname>[^\"]+)\"\s*>"
    r"(?P<pval>[\s\S]*?)"
    r"</\s*(?:antml:)?parameter\s*>",
    re.IGNORECASE,
)


def parse_text_tool_calls(text: str) -> list[dict]:
    """Extract text-form tool calls from prose.

    Returns ``[{"name": str, "input": {param: str}}]`` — one entry per
    ``<invoke>`` block in document order. Parameter values are RAW STRINGS
    (no type coercion here; the caller applies schema-driven coercion). A
    truncated trailing ``<invoke>`` with no closing tag still parses as long
    as it has complete ``<parameter>`` children. Returns ``[]`` (never raises)
    when nothing parseable is found.
    """
    if not text:
        return []
    out: list[dict] = []
    for m in _INVOKE_BLOCK_RE.finditer(text):
        name = (m.group("name") or "").strip()
        if not name:
            continue
        body = m.group("body") or ""
        params: dict[str, str] = {}
        for pm in _PARAM_RE.finditer(body):
            pname = (pm.group("pname") or "").strip()
            if not pname:
                continue
            pval = pm.group("pval")
            # Models routinely put the value on its own line. Drop ONE leading
            # and ONE trailing newline, but keep significant indentation (Bash
            # commands / Write payloads depend on it).
            if pval.startswith("\r\n"):
                pval = pval[2:]
            elif pval.startswith("\n"):
                pval = pval[1:]
            if pval.endswith("\n"):
                pval = pval[:-1]
            params[pname] = pval
        out.append({"name": name, "input": params})
    return out


def resolve_tool_name(emitted: str, advertised: list[str]) -> "str | None":
    """Resolve a model-emitted tool name to a registered one.

    1) exact match wins;
    2) else suffix match on the final ``__``-delimited segment
       (``brave_news_search`` → ``mcp__brave-search__brave_news_search``);
    3) ``None`` on zero matches OR more than one (ambiguous) — the caller then
       falls back to a corrective re-prompt rather than guess.
    """
    if not emitted:
        return None
    if emitted in advertised:
        return emitted
    candidates = [a for a in advertised if a.split("__")[-1] == emitted]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _coerce_value(value: str, ptype):
    """Best-effort coerce one string value to ``ptype``; keep raw on failure."""
    if ptype == "integer":
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if ptype == "number":
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if ptype == "boolean":
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        return value
    if ptype in ("object", "array"):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def coerce_tool_input(raw: dict, schema: "dict | None") -> dict:
    """Coerce string parameter values using the tool's input_schema.

    Coerce ONLY when the schema unambiguously names a non-string type for that
    property; on any failure the raw string is kept (``_validate_tool_input``
    then surfaces a clean InputValidationError the model can self-correct —
    strictly safer than dropping or guessing). Missing schema / unknown
    property → value passed through as-is.
    """
    if not raw:
        return {}
    props = {}
    if isinstance(schema, dict):
        p = schema.get("properties")
        if isinstance(p, dict):
            props = p
    out: dict = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            out[key] = value
            continue
        spec = props.get(key)
        ptype = spec.get("type") if isinstance(spec, dict) else None
        out[key] = _coerce_value(value, ptype)
    return out

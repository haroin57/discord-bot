"""Usage accounting and diagnostics for the Claude tool loop."""

import logging
import re

from services.claude_api.ratelimit import observe as observe_ratelimit


log = logging.getLogger("services.claude_api.tool_loop")


def _observe_response_headers(source) -> None:
    """Feed response rate-limit headers into shared pacing state."""
    try:
        response = getattr(source, "response", None)
        if response is not None and getattr(response, "headers", None) is not None:
            observe_ratelimit(response.headers)
    except Exception as exc:
        log.debug("ratelimit observe failed: %s", exc)


_HALLUC_VERBS = (
    "書きました", "書き出しました", "書き込みました", "保存しました",
    "作成しました", "作りました", "出力しました", "残しました", "記録しました",
    "wrote to", "wrote out", "saved to", "saved as", "created file",
    "written to",
)
_HALLUC_VERB_RE = re.compile(
    r"(?P<verb>" + "|".join(re.escape(verb) for verb in _HALLUC_VERBS) + r")",
    re.IGNORECASE,
)
_HALLUC_PATH_RE = re.compile(
    r"(?P<path>[\w./\\~-]+\."
    r"(?:md|py|txt|json|yaml|yml|toml|sh|rst|csv|tsv|html|css|js|ts|tsx|jsx|"
    r"go|rs|java|kt|swift|c|cpp|h|hpp|sql|env|ini|cfg|log))\b"
)
_HALLUC_MAX_DISTANCE = 160
_HALLUC_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _detect_write_hallucination(
    turn_text: str,
    fired_tools: set[str],
    *,
    channel_id: int,
    turn: int,
) -> None:
    """Log a warning when prose claims an unobserved file write."""
    if not turn_text or fired_tools & _HALLUC_WRITE_TOOLS:
        return
    verbs = list(_HALLUC_VERB_RE.finditer(turn_text))
    if not verbs:
        return
    paths = list(_HALLUC_PATH_RE.finditer(turn_text))
    if not paths:
        return
    best: tuple[int, re.Match, re.Match] | None = None
    for verb in verbs:
        for path in paths:
            distance = max(verb.start(), path.start()) - min(verb.end(), path.end())
            if distance < 0:
                distance = 0
            if distance > _HALLUC_MAX_DISTANCE:
                continue
            if best is None or distance < best[0]:
                best = (distance, verb, path)
    if best is None:
        return
    _, verb, path = best
    lo = min(verb.start(), path.start())
    hi = max(verb.end(), path.end())
    snippet = turn_text[lo:hi]
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    log.warning(
        "hallucination_suspect ch=%d turn=%d verb=%r path=%r snippet=%r "
        "fired_tools=%s",
        channel_id,
        turn + 1,
        verb.group("verb"),
        path.group("path"),
        snippet,
        sorted(fired_tools),
    )


_USAGE_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _usage_number(usage, field: str) -> int:
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(field, 0)
    else:
        value = getattr(usage, field, 0)
    return value if isinstance(value, int) else 0


_last_context_tokens: dict[int, int] = {}


def get_last_context_tokens(channel_id: int) -> int:
    """Return the latest observed context footprint for a channel."""
    return _last_context_tokens.get(channel_id, 0)


def _context_tokens(values: dict[str, int]) -> int:
    return (
        values["input_tokens"]
        + values["cache_read_input_tokens"]
        + values["cache_creation_input_tokens"]
    )


def _log_turn_usage(
    channel_id: int,
    turn: int,
    final_msg,
    totals: dict[str, int],
    *,
    model: str = "",
    request_id: str = "",
    duration_ms: int | None = None,
    ttft_ms: int | None = None,
    message_count: int = 0,
) -> None:
    """Account and log one completed API turn."""
    usage = getattr(final_msg, "usage", None)
    if usage is None:
        return
    values = {field: _usage_number(usage, field) for field in _USAGE_FIELDS}
    for field, value in values.items():
        totals[field] += value
    totals["turns"] += 1
    _last_context_tokens[channel_id] = _context_tokens(values)
    stop_reason = getattr(final_msg, "stop_reason", None)
    log.info(
        "claude_api_usage channel=%d turn=%d model=%s input_tokens=%d "
        "cache_read_input_tokens=%d cache_creation_input_tokens=%d "
        "output_tokens=%d stop_reason=%s request_id=%s duration_ms=%s "
        "ttft_ms=%s message_count=%d",
        channel_id,
        turn + 1,
        model or "-",
        values["input_tokens"],
        values["cache_read_input_tokens"],
        values["cache_creation_input_tokens"],
        values["output_tokens"],
        stop_reason,
        request_id or "-",
        duration_ms if duration_ms is not None else "-",
        ttft_ms if ttft_ms is not None else "-",
        message_count,
    )
    if stop_reason == "refusal":
        stop_details = getattr(final_msg, "stop_details", None)
        log.warning(
            "claude_api_refusal channel=%d turn=%d category=%s explanation=%r",
            channel_id,
            turn + 1,
            getattr(stop_details, "category", None),
            getattr(stop_details, "explanation", None),
        )


def _log_total_usage(channel_id: int, totals: dict[str, int]) -> None:
    """Log a tool-loop-wide summary for multi-turn requests."""
    if totals.get("turns", 0) <= 1:
        return
    log.info(
        "claude_api_usage_total channel=%d turns=%d input_tokens=%d "
        "cache_read_input_tokens=%d cache_creation_input_tokens=%d output_tokens=%d",
        channel_id,
        totals["turns"],
        totals["input_tokens"],
        totals["cache_read_input_tokens"],
        totals["cache_creation_input_tokens"],
        totals["output_tokens"],
    )

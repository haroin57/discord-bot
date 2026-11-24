"""Exception-safe structured logging with bounded recursive redaction."""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Mapping, Sequence


_UNAVAILABLE = "[unavailable]"
_REDACTED = "[REDACTED]"
_MAX_DEPTH = 5
_MAX_ITEMS = 50
_MAX_TEXT = 4096
_SENSITIVE_KEY = re.compile(
    r"authorization|cookie|credential|passwd|password|secret|token|api[_-]?key",
    re.IGNORECASE,
)
_AUTH_VALUE = re.compile(
    r"""(?ix)\b(bearer|basic)\s+(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,;\r\n}\]]+)"""
)
_HEADER_VALUE = re.compile(
    r"""(?ix)(?:["'])?\b(cookie|set-cookie|authorization)(?:["'])?\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,;\r\n}\]]+)"""
)
_NAMED_SECRET = re.compile(
    r"""(?ix)(?:["'])?\b([A-Za-z0-9_-]*(?:token|secret|password|passwd|api[_-]?key))(?:["'])?\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,;\r\n}\]]+)"""
)
_STRUCTURED_HANDLER_OWNER = "discord-bot-structured-json-v1"
_HANDLER_OWNER_ATTR = "_discord_bot_logging_owner"
_PREVIOUS_LEVEL_ATTR = "_discord_bot_previous_level"
_CONFIGURED_LEVEL_ATTR = "_discord_bot_configured_level"


def _redact_text(value: str) -> str:
    text = value[:_MAX_TEXT]
    if len(value) > _MAX_TEXT:
        text += "…[truncated]"
    text = _AUTH_VALUE.sub(lambda match: f"{match.group(1)} {_REDACTED}", text)
    text = _HEADER_VALUE.sub(lambda match: f"{match.group(1)}: {_REDACTED}", text)
    return _NAMED_SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", text)


def _safe_repr(value: object) -> str:
    try:
        rendered = repr(value)
    except BaseException:
        return _UNAVAILABLE
    return _redact_text(rendered)


def _redact_value(
    value: object,
    *,
    key: str | None = None,
    depth: int = 0,
    seen: set[int] | None = None,
) -> object:
    if key is not None and _SENSITIVE_KEY.search(key):
        return _REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bytes):
        return _redact_text(value.decode("utf-8", errors="replace"))
    if depth >= _MAX_DEPTH:
        return "[max-depth]"
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return "[cycle]"
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            output: dict[str, object] = {}
            for index, (nested_key, nested_value) in enumerate(value.items()):
                if index >= _MAX_ITEMS:
                    output["[truncated]"] = f"{len(value) - _MAX_ITEMS} more items"
                    break
                safe_key = (
                    _redact_text(nested_key)
                    if isinstance(nested_key, str)
                    else _safe_repr(nested_key)
                )
                output[safe_key] = _redact_value(
                    nested_value,
                    key=safe_key,
                    depth=depth + 1,
                    seen=seen,
                )
            return output
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return [
                _redact_value(item, depth=depth + 1, seen=seen)
                for item in value[:_MAX_ITEMS]
            ]
        if isinstance(value, (set, frozenset)):
            items = list(value)
            return [
                _redact_value(item, depth=depth + 1, seen=seen)
                for item in items[:_MAX_ITEMS]
            ]
        return _safe_repr(value)
    except BaseException:
        return _UNAVAILABLE
    finally:
        seen.discard(identity)


class JsonFormatter(logging.Formatter):
    """Format a LogRecord without allowing logging failures or secret leaks."""

    _RESERVED = {
        "rid",
        "ts",
        "level",
        "logger",
        "exc",
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except BaseException:
            message = "[message unavailable]"
        try:
            timestamp = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.gmtime(record.created),
            )
        except BaseException:
            timestamp = _UNAVAILABLE
        payload: dict[str, object] = {
            "ts": timestamp,
            "level": _redact_value(record.levelname),
            "logger": _redact_value(record.name),
            "msg": _redact_value(message),
        }
        try:
            from services.correlation import get_request_id

            payload["rid"] = _redact_value(get_request_id())
        except BaseException:
            payload["rid"] = _UNAVAILABLE
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = _redact_value(value, key=key)
        if record.exc_info:
            try:
                payload["exc"] = _redact_value(self.formatException(record.exc_info))
            except BaseException:
                payload["exc"] = "[exception unavailable]"
        try:
            return json.dumps(payload, ensure_ascii=False)
        except BaseException:
            return json.dumps(
                {
                    "ts": timestamp,
                    "level": _UNAVAILABLE,
                    "logger": _UNAVAILABLE,
                    "msg": "[record unavailable]",
                    "rid": _UNAVAILABLE,
                }
            )


def install_structured_logging(level: int = logging.INFO) -> logging.Handler:
    """Install or reuse the owned JSON handler without touching external handlers."""
    root = logging.getLogger()
    previous_level = root.level
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_OWNER_ATTR, None) != _STRUCTURED_HANDLER_OWNER:
            continue
        if getattr(existing, "_closed", False):
            previous_level = getattr(
                existing,
                _PREVIOUS_LEVEL_ATTR,
                previous_level,
            )
            root.removeHandler(existing)
            continue
        existing.setFormatter(JsonFormatter())
        root.setLevel(level)
        setattr(existing, _CONFIGURED_LEVEL_ATTR, level)
        return existing

    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_OWNER_ATTR, _STRUCTURED_HANDLER_OWNER)
    setattr(handler, _PREVIOUS_LEVEL_ATTR, previous_level)
    setattr(handler, _CONFIGURED_LEVEL_ATTR, level)
    root.addHandler(handler)
    return handler


# ---------------------------------------------------------------------------
# Third-party log-volume hygiene (OPT-B).
#
# In prod the journal is ~85% third-party request chatter: httpx (~52%, Chroma
# get/query + embeddings + R2 PUT) and aiohttp.access (~33%, the per-minute
# GET /health probe). We quiet those loggers but never *lose* the Chroma /
# embedding / R2 call counts: a counting filter on the httpx logger suppresses
# each raw ``HTTP Request: ...`` line and instead emits one aggregated
# ``[HTTP_CALLS]`` INFO line per request id.
# ---------------------------------------------------------------------------

_NOISE_LOGGERS = ("aiohttp.access", "httpcore")
_HTTPX_LOGGER = "httpx"
_HTTP_COUNTER_LOGGER = "kanae.http_calls"
_HTTP_REQUEST_PREFIX = "HTTP Request:"
_HTTP_CATEGORIES = ("get", "query", "embeddings", "put", "other")
_HTTP_COUNTER_FILTER_ATTR = "_discord_bot_http_counter_filter"
_MAX_TRACKED_RIDS = 256

_http_counts_lock = threading.Lock()
_http_counts: dict[str, dict[str, int]] = {}
_http_last_rid: list[str | None] = [None]
_atexit_registered = [False]


def _env_level(env_key: str, default: int) -> int:
    """Resolve a logging level from an env var (name or numeric), else default."""
    raw = os.environ.get(env_key)
    if not raw:
        return default
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    resolved = logging.getLevelName(raw.upper())
    return resolved if isinstance(resolved, int) else default


def _categorize_http_request(message: str) -> str | None:
    """Classify an httpx ``HTTP Request: <METHOD> <URL> "..."`` line.

    Returns one of ``_HTTP_CATEGORIES`` or ``None`` if the line is not an httpx
    request line (so other httpx chatter can be dropped without miscounting).
    """
    if not message.startswith(_HTTP_REQUEST_PREFIX):
        return None
    parts = message[len(_HTTP_REQUEST_PREFIX) :].split()
    if len(parts) < 2:
        return "other"
    method = parts[0].upper()
    path = parts[1].split("?", 1)[0].lower()
    if "/embeddings" in path:
        return "embeddings"
    if path.endswith("/query"):
        return "query"
    if path.endswith("/get"):
        return "get"
    if method == "PUT":
        return "put"
    return "other"


def _emit_http_counter(rid: str, counts: dict[str, int]) -> None:
    total = sum(counts.values())
    if total <= 0:
        return
    logging.getLogger(_HTTP_COUNTER_LOGGER).info(
        "[HTTP_CALLS] rid=%s get=%d query=%d embeddings=%d put=%d other=%d total=%d",
        rid,
        counts.get("get", 0),
        counts.get("query", 0),
        counts.get("embeddings", 0),
        counts.get("put", 0),
        counts.get("other", 0),
        total,
    )


def flush_http_counters(rid: str | None = None) -> None:
    """Emit and clear accumulated HTTP call counters.

    ``rid`` flushes just that request; ``None`` flushes everything (shutdown /
    atexit). A no-op when counting is disabled or nothing has accumulated.
    """
    pending: list[tuple[str, dict[str, int]]] = []
    with _http_counts_lock:
        if rid is None:
            keys = list(_http_counts.keys())
            _http_last_rid[0] = None
        elif rid in _http_counts:
            keys = [rid]
        else:
            keys = []
        for key in keys:
            counts = _http_counts.pop(key, None)
            if counts:
                pending.append((key, counts))
    for key, counts in pending:
        _emit_http_counter(key, counts)


class _HttpCallCounterFilter(logging.Filter):
    """Count httpx request lines per request id and suppress the raw line.

    Attached to the ``httpx`` logger. The per-request ``HTTP Request: ...`` INFO
    lines (Chroma/embeddings/R2 — the bulk of prod journal volume) never reach
    the handlers; instead a single ``[HTTP_CALLS]`` line per request id keeps the
    get/query/embeddings/PUT counts observable. WARNING+ records pass through so
    real transport failures are never swallowed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        try:
            message = record.getMessage()
        except BaseException:
            return False
        category = _categorize_http_request(message)
        if category is None:
            # Non-request httpx INFO/DEBUG chatter: drop to keep the journal quiet.
            return False
        try:
            from services.correlation import get_request_id

            rid = get_request_id()
        except BaseException:
            rid = "-"
        to_emit: list[tuple[str, dict[str, int]]] = []
        with _http_counts_lock:
            previous = _http_last_rid[0]
            if previous is not None and previous != rid:
                prev_counts = _http_counts.pop(previous, None)
                if prev_counts:
                    to_emit.append((previous, prev_counts))
            _http_last_rid[0] = rid
            counts = _http_counts.get(rid)
            if counts is None:
                counts = {name: 0 for name in _HTTP_CATEGORIES}
                _http_counts[rid] = counts
            counts[category] += 1
            while len(_http_counts) > _MAX_TRACKED_RIDS:
                oldest_key = next(iter(_http_counts))
                if oldest_key == rid:
                    break
                oldest = _http_counts.pop(oldest_key, None)
                if oldest:
                    to_emit.append((oldest_key, oldest))
        for key, emitted in to_emit:
            _emit_http_counter(key, emitted)
        return False


def configure_noisy_loggers() -> logging.Filter | None:
    """Quiet chatty third-party loggers without losing call-count visibility.

    - ``aiohttp.access`` (per-minute ``GET /health`` probes) and ``httpcore``
      are raised to WARNING.
    - ``httpx`` request lines are counted + suppressed by a filter that emits a
      per-request ``[HTTP_CALLS]`` summary, unless ``BOT_HTTP_COUNTER=0``, in
      which case httpx is simply raised to WARNING like the others.

    Each logger's level is overridable via ``BOT_LOG_LEVEL_<LOGGER>`` (dots ->
    ``_``, upper-cased), e.g. ``BOT_LOG_LEVEL_AIOHTTP_ACCESS=ERROR`` or
    ``BOT_LOG_LEVEL_HTTPX=INFO``. Idempotent. Returns the installed counter
    filter, or ``None``.
    """
    for name in _NOISE_LOGGERS:
        env_key = "BOT_LOG_LEVEL_" + name.upper().replace(".", "_")
        logging.getLogger(name).setLevel(_env_level(env_key, logging.WARNING))

    counter_enabled = os.environ.get("BOT_HTTP_COUNTER", "1").strip() != "0"
    httpx_logger = logging.getLogger(_HTTPX_LOGGER)

    if not counter_enabled:
        for existing in list(httpx_logger.filters):
            if getattr(existing, _HTTP_COUNTER_FILTER_ATTR, False):
                httpx_logger.removeFilter(existing)
        httpx_logger.setLevel(_env_level("BOT_LOG_LEVEL_HTTPX", logging.WARNING))
        return None

    # Counter mode: keep httpx emitting request lines (INFO) so the filter can
    # count them; still honour an explicit operator level override.
    httpx_logger.setLevel(_env_level("BOT_LOG_LEVEL_HTTPX", logging.INFO))

    for existing in httpx_logger.filters:
        if getattr(existing, _HTTP_COUNTER_FILTER_ATTR, False):
            return existing  # already installed; stay idempotent

    counter = _HttpCallCounterFilter()
    setattr(counter, _HTTP_COUNTER_FILTER_ATTR, True)
    httpx_logger.addFilter(counter)
    if not _atexit_registered[0]:
        atexit.register(flush_http_counters)
        _atexit_registered[0] = True
    return counter


__all__ = [
    "JsonFormatter",
    "configure_noisy_loggers",
    "flush_http_counters",
    "install_structured_logging",
]

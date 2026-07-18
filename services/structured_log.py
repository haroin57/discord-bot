"""Exception-safe structured logging with bounded recursive redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
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


__all__ = ["JsonFormatter", "install_structured_logging"]

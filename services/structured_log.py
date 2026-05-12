"""Structured JSON logging for production (SRE-H3).

Emits one JSON object per line to stderr/stdout, parseable by journalctl
+ jq. Automatically includes:
  - timestamp (ISO 8601)
  - level
  - logger name
  - message
  - request_id (from services.correlation)
  - extra kwargs passed to log calls

Usage:
    from services.structured_log import install_structured_logging
    install_structured_logging(level=logging.INFO)
    log.info("user message received", extra={"user_id": "123", "channel_id": 42})

The request_id field is auto-included via the contextvar set by
services.correlation.new_request_id().
"""

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    """Format LogRecord as a single JSON line."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process",
        "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        # Inject request_id if correlation module is available.
        try:
            from services.correlation import get_request_id
            payload["rid"] = get_request_id()
        except ImportError:
            payload["rid"] = "-"
        # Surface any extra= kwargs the caller passed.
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def install_structured_logging(level: int = logging.INFO) -> None:
    """Replace root handlers with a JSON-formatting StreamHandler."""
    root = logging.getLogger()
    root.setLevel(level)
    # Remove any pre-existing handlers (avoid duplicate output)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


__all__ = ["JsonFormatter", "install_structured_logging"]

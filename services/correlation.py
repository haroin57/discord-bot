"""Per-request correlation ID helpers (SRE-H4).

Used by bot.py / web_api.py to assign a UUID per inbound message and
thread it through logging via contextvars. Logging formatters (e.g.
services.structured_log) automatically include it in every log record.

Usage:
    from services.correlation import new_request_id, get_request_id
    # on entry to a request handler:
    rid = new_request_id()
    log.info("processing request")  # rid auto-included in JSON logs
"""

import contextvars
import uuid

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def new_request_id() -> str:
    """Generate a fresh request id, set as current context, return it."""
    rid = uuid.uuid4().hex[:8]
    _REQUEST_ID.set(rid)
    return rid


def get_request_id() -> str:
    """Return the current request id, or '-' if none set."""
    return _REQUEST_ID.get()


def set_request_id(rid: str) -> None:
    """Manually set (e.g. for tasks spawned outside a request handler)."""
    _REQUEST_ID.set(rid)


__all__ = ["new_request_id", "get_request_id", "set_request_id"]

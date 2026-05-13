"""Status callback helpers — safe invocation + tool-input hint extraction.

Ported from services/claude_sdk.py:308-354.
"""

import logging

log = logging.getLogger(__name__)


async def _safe_status(cb, msg: str) -> None:
    """Invoke status_callback without letting exceptions propagate."""
    if cb is None:
        return
    try:
        await cb(msg)
    except Exception as e:
        log.debug("status_callback failed (non-fatal): %s", e)


def _tool_input_hint(name: str, inp: dict | None) -> str:
    """Return a short human-readable hint for the Discord status message."""
    if not inp:
        return ""
    try:
        n = name.split("__")[-1] if "__" in name else name  # strip mcp prefix
        if n in ("Bash", "BashOutput"):
            return inp.get("command", "")
        if n in ("Read",):
            return inp.get("file_path", "")
        if n in ("Edit", "Write", "MultiEdit"):
            return inp.get("file_path", "")
        if n in ("Glob",):
            return inp.get("pattern", "")
        if n in ("Grep",):
            return inp.get("pattern", "")
        if n in ("WebFetch", "navigate"):
            return inp.get("url", "")
        if n in ("WebSearch", "brave_web_search", "brave_local_search"):
            return inp.get("query", "")
        if n in ("search_x",):
            return inp.get("query", "")
        if n in ("Agent",):
            return inp.get("description", "")
        # Generic: show first string value
        for v in inp.values():
            if isinstance(v, str) and v:
                return v
        return ""
    except Exception:
        return ""

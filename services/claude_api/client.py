"""Shared client-side constants used by both __init__ and entry.

Lifted to its own module so submodules (entry.py, tool_loop.py) can import
these constants without going through the package __init__ which itself
imports those submodules — a circular import.
"""

import hashlib
import json
import logging
import os
import uuid

log = logging.getLogger(__name__)

CC_VERSION = "2.1.92"
CC_ENTRYPOINT = "cli"  # Matches the real claude-cli User-Agent suffix.

# User-Agent the real CLI uses. Captured via mitmproxy on /v1/messages from
# claude-cli 2.1.92. The desktop bundle ships "Claude-Desktop" but the CLI
# is what we want to mimic since we have the same surface shape (no GUI).
USER_AGENT = f"claude-cli/{CC_VERSION} (external, {CC_ENTRYPOINT})"

# Minimal subscriber-tier beta set. Per spacewander_lzx's "Anthropic Beta
# Header 全景" writeup, several beta headers REQUIRE matching body fields:
#   interleaved-thinking-2025-05-14 → body needs "thinking": {"type": ...}
#   effort-2025-11-24               → body needs "output_config": {effort: }
#   context-management-2025-06-27   → body needs "context_management": {...}
#   advanced-tool-use-2025-11-20    → tool schemas need defer_loading
#   redact-thinking-2026-02-12      → requires thinking enabled
# Advertising them without the matching body fields triggers 429 / 400.
# We currently send a plain messages payload, so we keep only the four
# subscriber-essential betas and add the others back as we add the body
# fields they require.
MODEL_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "context-1m-2025-08-07",
    "prompt-caching-scope-2026-01-05",
]

_session_id = str(uuid.uuid4())


# Identity prefix the real CLI sends as system[1] (mitmproxy capture
# 2026-05-13). Must be sent verbatim — server-side OAuth-subscriber
# gating appears to inspect this string.
CLI_IDENTITY_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."


def _read_claude_user_account() -> dict:
    """Return ``{"device_id": str, "account_uuid": str}`` for metadata.user_id.

    Read from ``~/.claude.json`` which the official CLI maintains. The CLI
    stores ``userID`` (a 64-hex device fingerprint) at the top level and
    ``oauthAccount.accountUuid`` nested. Both are required by the API for
    subscriber-tier identification; sending only ``session_id`` triggers
    HTTP 429 because the request looks like an anonymous OAuth call.

    Cached in-memory after first read so the JSON parse cost is paid once.
    """
    global _user_account_cache
    if _user_account_cache is not None:
        return _user_account_cache
    path = os.path.expanduser("~/.claude.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", path, exc)
        _user_account_cache = {}
        return _user_account_cache
    device_id = data.get("userID", "")
    account_uuid = ""
    oauth_account = data.get("oauthAccount")
    if isinstance(oauth_account, dict):
        account_uuid = oauth_account.get("accountUuid", "") or ""
    _user_account_cache = {"device_id": device_id, "account_uuid": account_uuid}
    return _user_account_cache


_user_account_cache: dict | None = None


def get_metadata() -> dict:
    """Build the full ``metadata`` object the API expects from OAuth subscribers.

    The CLI sends a JSON-encoded ``user_id`` containing device_id +
    account_uuid + session_id. Anything less and the API does not recognize
    the call as subscriber-tier (observed: HTTP 429 instead of 200).
    """
    account = _read_claude_user_account()
    payload = {"session_id": _session_id}
    if account.get("device_id"):
        payload["device_id"] = account["device_id"]
    if account.get("account_uuid"):
        payload["account_uuid"] = account["account_uuid"]
    return {"user_id": json.dumps(payload)}


def get_attribution_header_value(messages_str: str) -> str:
    """Build the ``x-anthropic-billing-header`` value (no name prefix).

    The CLI also embeds this string verbatim as ``system[0]`` — that
    redundancy is part of the subscriber-tier protocol. The trailing
    ``cch=00000;`` segment is sent by the real CLI; we mirror it.
    """
    fingerprint = hashlib.sha256(
        (messages_str + CC_VERSION).encode()
    ).hexdigest()[:16]
    return (
        f"cc_version={CC_VERSION}.{fingerprint}; "
        f"cc_entrypoint={CC_ENTRYPOINT}; cch=00000;"
    )

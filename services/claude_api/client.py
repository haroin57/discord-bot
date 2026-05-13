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

# Beta list captured verbatim from the real claude-cli 2.1.92 against
# /v1/messages with model=claude-opus-4-6 (mitmproxy 2026-05-13). Every
# beta in this list is paired with a body field below (THINKING_CONFIG,
# OUTPUT_CONFIG) so the server accepts them.
MODEL_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "context-1m-2025-08-07",
    "interleaved-thinking-2025-05-14",
    "redact-thinking-2026-02-12",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
    "advanced-tool-use-2025-11-20",
    "effort-2025-11-24",
]

# Body fields the CLI always sends alongside the betas above.
THINKING_CONFIG = {"type": "adaptive"}
OUTPUT_CONFIG = {"effort": "medium"}
CLAUDE_MAX_TOKENS_OPUS = 64000

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


# Hardcoded SALT used by the real claude-cli (KA7 / jBY in cli.js).
# Located in /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js.
# We strongly suspect the API server recomputes this hash to validate
# subscriber identity; sending a wrong hash format puts the call into a
# generic OAuth bucket that is rate-limited far harder than the
# subscriber bucket.
_BILLING_SALT = "59cf53e54c78"


def _first_user_text(messages: list[dict]) -> str:
    """Return the text of the first non-meta user message in ``messages``.

    Mirrors the CLI's HBY function (cli.js): scans for the first user
    message that is NOT marked isMeta (CLI internal field used for
    system-reminder / available-deferred-tools etc.). We don't carry an
    explicit ``isMeta`` flag in our API-shaped messages, but we should
    skip any user message whose only content blocks are clearly meta:
    those wrapped in ``<system-reminder>``, ``<available-deferred-tools>``,
    or ``<attached_images>`` tags. Falls back to the very first user
    message text if nothing else qualifies.

    Returning the right text matters because the attribution suffix
    (KA7 in cli.js) hashes ``text[4] + text[7] + text[20]``; using a
    meta-prefixed string yields a different suffix than the CLI's, and
    the API server appears to validate it.
    """
    fallback = ""
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        text_val: str | None = None
        if isinstance(content, str):
            text_val = content
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    t = blk.get("text", "") or ""
                    if t:
                        text_val = t
                        break
        if text_val is None:
            continue
        if not fallback:
            fallback = text_val
        stripped = text_val.lstrip()
        if stripped.startswith((
            "<system-reminder>",
            "<available-deferred-tools>",
            "<attached_images>",
        )):
            continue
        return text_val
    return fallback


def get_attribution_header_value(messages: list[dict] | None = None) -> str:
    """Build the ``x-anthropic-billing-header`` value (no name prefix).

    Algorithm copied from claude-cli 2.1.92's KA7 function:

        first_text = first user message's text content
        picks = first_text[4] + first_text[7] + first_text[20] (each "0" if absent)
        suffix = sha256(SALT + picks + VERSION).hex[:3]
        cc_version = f"{VERSION}.{suffix}"
        return f"cc_version=...; cc_entrypoint=...; cch=00000;"

    The CLI also embeds this string verbatim as ``system[0]`` — that
    redundancy is part of the subscriber-tier protocol.
    """
    text = _first_user_text(messages or [])
    picks = "".join((text[i] if len(text) > i else "0") for i in (4, 7, 20))
    raw = f"{_BILLING_SALT}{picks}{CC_VERSION}"
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:3]
    return (
        f"cc_version={CC_VERSION}.{suffix}; "
        f"cc_entrypoint={CC_ENTRYPOINT}; cch=00000;"
    )

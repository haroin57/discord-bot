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

# === Auto-captured CLI identity block ===
# Values below are refreshed by scripts/refresh_cli_identity.sh (cron'd to
# run daily). DO NOT edit between the marker comments; manual edits will be
# clobbered on the next refresh run.
# AUTO-CAPTURE-START
CC_VERSION = "2.1.156"
CC_ENTRYPOINT = "sdk-cli"
MODEL_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
    "advanced-tool-use-2025-11-20",
    "effort-2025-11-24",
    "extended-cache-ttl-2025-04-11",
]
CLI_IDENTITY_TEXT = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
# AUTO-CAPTURE-END

USER_AGENT = f"claude-cli/{CC_VERSION} (external, {CC_ENTRYPOINT})"

# Body fields the CLI always sends alongside the betas above. These are
# pinned verbatim from the 2026-05-14 mitm capture of claude-cli 2.1.92
# against /v1/messages so the bot's request byte-shape matches the
# subscriber-tier identification path.
#
# - thinking.type = "adaptive": let the model spend as much thinking
#   budget as it judges necessary (no fixed cap below max_tokens).
# - output_config.effort = "max": the absolute top of Anthropic's effort
#   dial (low < medium < high < xhigh < max). Available on Opus 4.6 / 4.7
#   under the effort-2025-11-24 beta. The mitm capture of claude-cli
#   2.1.92 shows "high" because that's the CLI's default, but the dial
#   actually goes higher — the model spends more thinking + tool calls
#   per turn at "max" in exchange for slower / pricier responses. The
#   bot opts in deliberately (haroin preference: correctness > cost).
# - context_management.clear_thinking_20251015 with keep="all": the
#   server-side bookkeeping the CLI requests for the new
#   advanced-tool-use beta. Mirroring it keeps overage-cohort routing
#   working under sustained load.
THINKING_CONFIG = {"type": "adaptive"}
OUTPUT_CONFIG = {"effort": "max"}
CONTEXT_MANAGEMENT_CONFIG = {
    "edits": [
        {"type": "clear_thinking_20251015", "keep": "all"},
    ],
}
CLAUDE_MAX_TOKENS_OPUS = 64000

_session_id = str(uuid.uuid4())


def supports_adaptive_thinking(model: str) -> bool:
    """Whether ``model`` accepts ``thinking.type=adaptive``.

    Haiku returns HTTP 400 when adaptive thinking is requested, which was
    silently failing-open every Mem0 helper (query-expansion / dedup /
    rerank — all default to Haiku) and falling through to their except
    branches. Exclude Haiku (and any future small model carrying "haiku"
    in its id); Opus / Sonnet support adaptive thinking.
    """
    return "haiku" not in (model or "").lower()


def _load_cli_default_system_prompt() -> str:
    """The CLI's default agent system prompt (system[2] in mitm capture).

    Pinned verbatim from the real claude-cli 2.1.92 against /v1/messages
    on model=claude-opus-4-6. Sending this block at system[2] (with
    ``cache_control={"type":"ephemeral","scope":"global"}``) appears to
    be required for subscriber-tier identification — without it the
    server returns 429 with zero ratelimit-unified headers.
    """
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__),
                         "cli_default_system_prompt.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        log.warning("could not load CLI default system prompt at %s: %s",
                    path, exc)
        return ""


CLI_DEFAULT_SYSTEM_PROMPT = _load_cli_default_system_prompt()


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
    """Build the ``metadata`` object the API expects from OAuth subscribers.

    Byte-exact mimic of the CLI: the ``user_id`` JSON string is built with
    keys in the order ``device_id, account_uuid, session_id`` and with
    ``json.dumps(..., separators=(',', ':'))`` so there are no spaces. We
    suspect the server hashes / compares this string verbatim for
    subscriber identification — Python's default ``json.dumps`` reorders
    by insertion and inserts spaces, both of which differ from JS's
    ``JSON.stringify`` output.
    """
    account = _read_claude_user_account()
    # The order matters: CLI sends device_id first, then account_uuid, then
    # session_id. We construct an OrderedDict-equivalent via insertion order.
    # Free-code always includes device_id and account_uuid (even if empty
    # string). Omitting them changes the JSON shape and may affect server-
    # side subscriber identification.
    payload: dict[str, str] = {
        "device_id": account.get("device_id", ""),
        "account_uuid": account.get("account_uuid", ""),
        "session_id": _session_id,
    }
    return {"user_id": json.dumps(payload, separators=(",", ":"))}


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


def _generate_cch() -> str:
    """Return the cch=00000 placeholder for attestation.

    The real Claude Code CLI (Bun binary) computes xxHash64 of the full
    serialized request body and replaces this placeholder at the native
    HTTP layer. Our Python implementation does the same via an httpx
    event hook in attestation.py:cch_request_hook().

    Returning "00000" here means the system prompt carries the correct
    placeholder that the hook will patch before the request is sent.
    """
    return "00000"


def get_attribution_header_value(messages: list[dict] | None = None) -> str:
    """Build the full ``x-anthropic-billing-header: ...`` string.

    Algorithm copied from claude-cli 2.1.92's KA7 function (cli.js):

        first_text = first user message's text content (HBY-filtered)
        picks = first_text[4] + first_text[7] + first_text[20] ("0" if absent)
        suffix = sha256(SALT + picks + VERSION).hex[:3]
        cch = xxHash64(body, seed) & 0xFFFFF  // computed by Bun/Zig nativeFetch

    We emit cch=00000 here; attestation.py:cch_request_hook() patches
    it with the real xxHash64 value before the request leaves the process.

    The CLI embeds this string verbatim as ``system[0]`` INCLUDING
    the ``x-anthropic-billing-header: `` prefix (mitmproxy capture
    2026-05-14: system[0].text starts with that literal).
    """
    text = _first_user_text(messages or [])
    picks = "".join((text[i] if len(text) > i else "0") for i in (4, 7, 20))
    raw = f"{_BILLING_SALT}{picks}{CC_VERSION}"
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:3]
    cch = _generate_cch()
    return (
        f"x-anthropic-billing-header: cc_version={CC_VERSION}.{suffix}; "
        f"cc_entrypoint={CC_ENTRYPOINT}; cch={cch};"
    )

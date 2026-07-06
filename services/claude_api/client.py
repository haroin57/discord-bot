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

from config import CLAUDE_EFFORT

log = logging.getLogger(__name__)

# === Auto-captured CLI identity block ===
# Values below are refreshed by scripts/refresh_cli_identity.sh (cron'd to
# run daily). DO NOT edit between the marker comments; manual edits will be
# clobbered on the next refresh run.
# AUTO-CAPTURE-START
CC_VERSION = "2.1.177"
CC_ENTRYPOINT = "sdk-cli"
MODEL_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
    "mid-conversation-system-2026-04-07",
    "advanced-tool-use-2025-11-20",
    "effort-2025-11-24",
    "extended-cache-ttl-2025-04-11",
]
CLI_IDENTITY_TEXT = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
# AUTO-CAPTURE-END

# Supplementary betas appended OUTSIDE the auto-capture block so the daily
# refresh_cli_identity.sh run doesn't clobber them. context-1m-2025-08-07
# opens the 1M-token context window; without it the effective window is 200K
# and the 500K compaction trigger (config.HISTORY_COMPACT_TRIGGER_TOKENS) would
# overflow before compaction ever fires. Gated on CONTEXT_1M_ENABLED so it can
# be turned off in one place if the subscription/model rejects the beta.
_EXTRA_BETAS = []
if os.environ.get("CONTEXT_1M_ENABLED", "1") not in ("0", "false", "False", ""):
    _EXTRA_BETAS.append("context-1m-2025-08-07")
# Server-side model fallback betas (ADR-0015). A minimal `claude -p` capture
# does NOT emit these (no fallback configured in that ping), so they live OUTSIDE
# the AUTO-CAPTURE block — otherwise a refresh would strip them and break the
# server-side `fallbacks` body field used for the (kept) Fable refusal fallback.
_EXTRA_BETAS.append("server-side-fallback-2026-06-01")
_EXTRA_BETAS.append("fallback-credit-2026-06-01")
# Advisor tool（熟考 P4）。ADVISOR_ENABLED=true のときだけ beta を付与する
# （フラグ OFF で従来 wire-shape 完全維持）。
if os.environ.get("ADVISOR_ENABLED", "false").lower() == "true":
    _EXTRA_BETAS.append("advisor-tool-2026-03-01")
for _b in _EXTRA_BETAS:
    if _b not in MODEL_BETAS:
        MODEL_BETAS.append(_b)

USER_AGENT = f"claude-cli/{CC_VERSION} (external, {CC_ENTRYPOINT})"

# Body fields the CLI always sends alongside the betas above. These stay
# hand-pinned (the AUTO-CAPTURE block above tracks CC_VERSION/betas/identity
# from the live capture, but these body fields are not auto-extracted) to match
# the live claude-cli request byte-shape on the subscriber-tier identification
# path.
#
# - thinking.type = "adaptive": let the model spend as much thinking budget as
#   it judges necessary (no fixed cap below max_tokens). Kept byte-identical to
#   the real claude-cli, which sends {"type":"adaptive"} with no display field.
#   (We briefly tried display="summarized" while debugging fable-5 refusals, but
#   the real fix was output_config reaching the wire (ADR-0009), so we reverted
#   to match the CLI exactly.)
# - output_config.effort: env-driven via CLAUDE_EFFORT (.env pins "max" for
#   Opus 4.8 — haroin 2026-06-20). Valid tiers: low|medium|high|xhigh|max;
#   opus-4-8 accepts "max" (verified by a live HTTP 200). The CLI's own 4.8
#   default is "high". CLAUDE_CODE_EFFORT_LEVEL is also honored.
# - context_management.clear_thinking_20251015 with keep="all": the
#   server-side bookkeeping the CLI requests for the new
#   advanced-tool-use beta. Mirroring it keeps overage-cohort routing
#   working under sustained load.
THINKING_CONFIG = {"type": "adaptive"}
OUTPUT_CONFIG = {"effort": CLAUDE_EFFORT}
CONTEXT_MANAGEMENT_CONFIG = {
    # CLI parity (2026-06-20): the real claude-cli 2.1.177 sends ONLY
    # clear_thinking_20251015 keep:all — it sends NO clear_tool_uses edit. The
    # previous clear_tool_uses_20250919 block (trigger 150k, keep 5) was a
    # bot-only extension and a request-shape divergence (possible ratelimit-
    # cohort difference), so it was removed for strict CLI byte-parity. Old
    # tool-result bloat is handled by context-1m (1M window) + history
    # compaction (config.HISTORY_COMPACT_*), not by a server-side edit.
    "edits": [
        {"type": "clear_thinking_20251015", "keep": "all"},
    ],
}
CLAUDE_MAX_TOKENS_OPUS = 64000  # claude-cli opus-4-8 default (2026-06-20)

_session_id = str(uuid.uuid4())


def _load_cli_default_system_prompt() -> str:
    """The CLI's default agent system prompt (system[2] in the mitm capture).

    Auto-refreshed from a LIVE claude-cli /v1/messages capture by
    scripts/apply_cli_identity.py (last: 2.1.177 / claude-opus-4-8, 2026-06-20;
    replaced a hand-fabricated stand-in). Sending this block at system[2] (with
    ``cache_control={"type":"ephemeral","scope":"global"}``) is part of the
    subscriber-tier identification path — without it the server returns 429 with
    zero ratelimit-unified headers. Do NOT hand-edit cli_default_system_prompt.txt;
    the refresh overwrites it.
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


FAILOVER_ACCOUNT_SIDECAR = os.path.expanduser("~/.claude/.account-failover.json")


def _read_claude_user_account() -> dict:
    """Return ``{"device_id": str, "account_uuid": str}`` for metadata.user_id.

    Read from ``~/.claude.json`` which the official CLI maintains. The CLI
    stores ``userID`` (a 64-hex device fingerprint) at the top level and
    ``oauthAccount.accountUuid`` nested. Both are required by the API for
    subscriber-tier identification; sending only ``session_id`` triggers
    HTTP 429 because the request looks like an anonymous OAuth call.

    OAuth 二重アカウント対応: failover アカウントが active のときは
    サイドカー ``~/.claude/.account-failover.json``（同スキーマ、移行時に
    旧 ~/.claude.json から抽出）を読む。accountUuid はアカウント固有で、
    primary の uuid を failover token に添えると subscriber 識別が崩れて
    429 を誘発し得るため。device_id は同一マシン指紋なのでサイドカーに
    無ければ primary の値を流用する。account 別にキャッシュ。
    """
    try:
        from services.oauth_manager import get_active_account
        account_name = get_active_account()
    except Exception:  # noqa: BLE001
        account_name = "primary"
    cached = _user_account_cache.get(account_name)
    if cached is not None:
        return cached

    def _load(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read %s: %s", path, exc)
            return {}
        device_id = data.get("userID", "")
        account_uuid = ""
        oauth_account = data.get("oauthAccount")
        if isinstance(oauth_account, dict):
            account_uuid = oauth_account.get("accountUuid", "") or ""
        return {"device_id": device_id, "account_uuid": account_uuid}

    primary = _user_account_cache.get("primary")
    if primary is None:
        primary = _load(os.path.expanduser("~/.claude.json"))
        _user_account_cache["primary"] = primary

    if account_name == "failover":
        side = _load(FAILOVER_ACCOUNT_SIDECAR)
        result = {
            "device_id": side.get("device_id") or primary.get("device_id", ""),
            "account_uuid": side.get("account_uuid", ""),
        }
        _user_account_cache["failover"] = result
        return result
    return primary


_user_account_cache: dict[str, dict] = {}


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

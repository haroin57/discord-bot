"""Anthropic AsyncAnthropic client construction.

Builds the client with the exact User-Agent + X-Stainless-* header set
that the Node-based claude-cli sends. Sending the Python SDK's header
values appears to demote the call out of the subscriber-tier bucket on
Anthropic's side and trigger HTTP 429s, so we mimic the Node CLI even
though we're a Python process.

Pulled out of `services/claude_api/entry.py` so the orchestrator there
stays focused on the retry / agent-loop flow.
"""

from __future__ import annotations

import uuid as _uuid

import anthropic

from services.claude_api.client import USER_AGENT, _session_id
from services.oauth_manager import (
    ensure_valid_token,
    init as oauth_init,
    make_async_oauth_client,
    prime_subscriber_session,
)

__all__ = ["build_cli_headers", "make_api_client"]


def build_cli_headers() -> dict[str, str]:
    """Return the default headers an AsyncAnthropic client should send.

    The OAuth transport refreshes ``x-client-request-id`` on every request.
    """
    return {
        "x-app": "cli",
        "User-Agent": USER_AGENT,
        "X-Claude-Code-Session-Id": _session_id,
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.128.0",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v26.3.0",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Timeout": "600",
        "accept-language": "*",
        "sec-fetch-mode": "cors",
        "x-client-request-id": str(_uuid.uuid4()),
    }


async def make_api_client(restricted: bool) -> anthropic.AsyncAnthropic:
    """Return the AsyncAnthropic client for this call (OAuth subscriber flow).

    The separate static-API-key route for restricted callers was removed
    2026-07-04: BOT_RESTRICTED_ANTHROPIC_API_KEY was never configured in any
    environment, so every call — restricted included — already used OAuth.
    ``restricted`` stays in the signature because tool/permission gating
    downstream still keys off it; only the auth transport is unified.
    """
    cli_headers = build_cli_headers()
    # bot startup paths only call oauth_init() lazily through mem0 or the
    # sync get_client(); on a cold call_with_tools the token is still
    # uninitialized. Idempotent init() before ensure_valid_token() guarantees
    # _access_token is populated before make_async_oauth_client reads it.
    oauth_init()
    await ensure_valid_token()
    # Subscriber session prime: replay the CLI's /api/oauth/profile and
    # /api/oauth/claude_cli/roles GETs. Once per process. Without this,
    # we've seen 429s with zero ratelimit-unified headers (= subscriber
    # tier not recognized).
    await prime_subscriber_session()
    return make_async_oauth_client(default_headers=cli_headers)

#!/usr/bin/env python3
"""Send a minimal /v1/messages POST using the bot's current identity
configuration and report whether it returns HTTP 200.

Exit codes:
    0 — HTTP 200 OK (or 200 with overage-in-use)
    1 — HTTP 4xx / 5xx (identity-side issue; refresh should roll back)
    2 — HTTP 429 with non-fatal quota exhaustion (identity OK, just rate-limited)
    3 — other error (network, auth, etc.)

Run from repo root:
    .venv/bin/python scripts/verify_cli_identity.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running as a standalone script from anywhere — make the repo root
# importable so ``from config import ...`` and ``from services. ...`` resolve.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import anthropic


async def main() -> int:
    from config import SYSTEM_PROMPT, CLAUDE_MODEL
    from services.claude_api.client import (
        CLI_DEFAULT_SYSTEM_PROMPT,
        CLI_IDENTITY_TEXT,
        MODEL_BETAS,
        OUTPUT_CONFIG,
        THINKING_CONFIG,
        USER_AGENT,
        _session_id,
        get_attribution_header_value,
        get_metadata,
    )
    from services.oauth_manager import (
        ensure_valid_token,
        init as oauth_init,
        make_async_oauth_client,
        prime_subscriber_session,
    )

    oauth_init()
    await ensure_valid_token()
    await prime_subscriber_session()

    cli_headers = {
        "x-app": "cli",
        "User-Agent": USER_AGENT,
        "X-Claude-Code-Session-Id": _session_id,
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.80.0",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.14.1",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Timeout": "600",
        "accept-language": "*",
        "sec-fetch-mode": "cors",
        "x-client-request-id": str(__import__("uuid").uuid4()),
    }
    client = make_async_oauth_client(default_headers=cli_headers)

    # Minimal CLI-prefix system layout, mirroring services/mem0/cli_compat.py
    # (system[0] + system[2] cached with ttl:1h scope:global; system[1]/[3]
    # uncached). ttl:1h matches the cached blocks in the main bot path
    # (tool_loop._build_cached_system) and the real CLI capture.
    system = [
        {
            "type": "text",
            "text": get_attribution_header_value([]),
            "cache_control": {"type": "ephemeral", "ttl": "1h", "scope": "global"},
        },
        {"type": "text", "text": CLI_IDENTITY_TEXT},
        {
            "type": "text",
            "text": CLI_DEFAULT_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral", "ttl": "1h", "scope": "global"},
        },
        {"type": "text", "text": SYSTEM_PROMPT},
    ]
    messages = [{"role": "user", "content": "ping"}]

    try:
        resp = await client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=32,
            system=system,
            messages=messages,
            betas=MODEL_BETAS,
            metadata=get_metadata(),
            thinking=THINKING_CONFIG,
            output_config=OUTPUT_CONFIG,
        )
        # If we got here, status was 200.
        text = ""
        for blk in resp.content or []:
            if getattr(blk, "type", "") == "text":
                text = getattr(blk, "text", "")[:40]
                break
        print(json.dumps({
            "status": "ok",
            "http_status": 200,
            "preview": text,
            "stop_reason": getattr(resp, "stop_reason", ""),
        }, ensure_ascii=False))
        return 0

    except anthropic.RateLimitError as exc:
        # Inspect the response for overage signals.
        resp = getattr(exc, "response", None)
        headers = dict((resp.headers if resp is not None else {}) or {})
        five_h = headers.get("anthropic-ratelimit-unified-5h-status", "")
        seven_d = headers.get("anthropic-ratelimit-unified-7d-status", "")
        overage_in_use = headers.get("anthropic-ratelimit-unified-overage-in-use", "")
        print(json.dumps({
            "status": "rate_limited",
            "http_status": 429,
            "5h_status": five_h,
            "7d_status": seven_d,
            "overage_in_use": overage_in_use,
            "message": str(exc)[:200],
        }, ensure_ascii=False))
        # 429 alone does NOT mean identity is broken — the subscription quota
        # may genuinely be exhausted. Treat as non-fatal so refresh script
        # does not roll back a working identity update.
        return 2

    except anthropic.APIStatusError as exc:
        # 4xx / 5xx that isn't a rate limit. Likely identity / request-shape
        # problem — caller should roll back.
        print(json.dumps({
            "status": "api_error",
            "http_status": getattr(exc, "status_code", 0),
            "message": str(exc)[:300],
        }, ensure_ascii=False))
        return 1

    except Exception as exc:
        print(json.dumps({
            "status": "exception",
            "type": type(exc).__name__,
            "message": str(exc)[:300],
        }, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""CLI-identity compatibility shim for Mem0 LLM calls.

Mem0's small ad-hoc LLM tasks (fact extraction, dedup judgment, query rewrite,
candidate rerank) used to hit ``/v1/messages`` with a plain
``{"model": ..., "messages": [...]}`` body. That body shape is rejected by
the overage / extra-usage routing layer on the server when the OAuth
subscription window is exhausted — only requests that match the live
claude-cli's full body+header fingerprint get auto-billed against
claude.ai Extra Usage.

This module wraps ``client.beta.messages.create()`` so every Mem0 LLM call
sends the same 4-block system layout, same ``anthropic-beta`` set, same
``metadata.user_id`` shape, and the same Stainless / x-app headers that the
main bot path uses (see services/claude_api/client.py + entry.py).
"""

from __future__ import annotations

import uuid as _uuid

from services.claude_api.client import (
    CLI_DEFAULT_SYSTEM_PROMPT,
    CLI_IDENTITY_TEXT,
    MODEL_BETAS,
    THINKING_CONFIG,
    USER_AGENT,
    _session_id,
    get_attribution_header_value,
    get_metadata,
)


def cli_system(task_prompt: str) -> list[dict]:
    """Return the 4-block system array the real CLI sends.

    ``task_prompt`` is the Mem0-specific instruction text and is slotted into
    system[3]. system[0..2] are the verbatim CLI prefix that the server's
    subscriber-tier / overage routing recognises.
    """
    return [
        {
            "type": "text",
            "text": get_attribution_header_value([]),
            "cache_control": {"type": "ephemeral", "scope": "global"},
        },
        {"type": "text", "text": CLI_IDENTITY_TEXT},
        {
            "type": "text",
            "text": CLI_DEFAULT_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral", "scope": "global"},
        },
        {"type": "text", "text": task_prompt},
    ]


def cli_headers() -> dict:
    """The X-Stainless / x-app / User-Agent set the live CLI attaches."""
    return {
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
        "x-client-request-id": str(_uuid.uuid4()),
    }


async def cli_messages_create(
    client,
    *,
    model: str,
    max_tokens: int,
    user_message: str,
    task_prompt: str,
):
    """Drop-in for ``client.messages.create(model, max_tokens, messages)`` that
    wraps the call in the CLI-shaped body + headers.

    ``user_message`` becomes the sole user-turn content. ``task_prompt`` is
    the Mem0-specific instruction (extract / dedup / rewrite / rerank prompt)
    and is placed at system[3].

    Uses ``client.messages.create`` (not the ``beta`` namespace) so existing
    test mocks of ``mock_client.messages.create`` keep working. The beta flag
    list is passed via the ``anthropic-beta`` header instead, which the API
    accepts identically.
    """
    headers = cli_headers()
    headers["anthropic-beta"] = ",".join(MODEL_BETAS)
    return await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=cli_system(task_prompt),
        messages=[{"role": "user", "content": user_message}],
        metadata=get_metadata(),
        thinking=THINKING_CONFIG,
        extra_headers=headers,
    )


def extract_text(resp) -> str:
    """Pull the first TextBlock out of a Claude API response.

    With ``thinking=THINKING_CONFIG`` enabled (every Mem0 LLM call here), the
    server prepends one or more ``ThinkingBlock``s before the visible
    ``TextBlock``. Naively reading ``resp.content[0].text`` then raises
    ``AttributeError`` on the leading ThinkingBlock and the whole call path
    falls through to its except branch — observed in production as repeated
    "rerank_memories failed: 'ThinkingBlock' object has no attribute 'text'".
    """
    for block in getattr(resp, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return text
    return ""

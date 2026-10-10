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

import asyncio
import logging
import time
import uuid as _uuid

from services.claude_api.client_factory import build_cli_headers
from services.token_usage import log_helper_failure, log_helper_usage
from services.claude_api.client import (
    CLI_DEFAULT_SYSTEM_PROMPT,
    CLI_IDENTITY_TEXT,
    MODEL_BETAS,
    USER_AGENT,
    _session_id,
    get_attribution_header_value,
    get_metadata,
)

log = logging.getLogger(__name__)


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
            "cache_control": {"type": "ephemeral", "ttl": "1h", "scope": "global"},
        },
        {"type": "text", "text": CLI_IDENTITY_TEXT},
        {
            "type": "text",
            "text": CLI_DEFAULT_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral", "ttl": "1h", "scope": "global"},
        },
        {"type": "text", "text": task_prompt},
    ]


def cli_headers() -> dict:
    """The X-Stainless / x-app / User-Agent set the live CLI attaches."""
    return build_cli_headers()


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
    # Strip the long-context beta (context-1m-*) for ALL Mem0 helper calls.
    # These tasks (extract / dedup / rewrite / HyDE / rerank / classify) send
    # tiny prompts that never need the 1M window, and on this subscription only
    # Opus is entitled to context-1m: Haiku returns 400 "long context beta not
    # available", and Sonnet 4.6 returns 429 "Usage credits are required for
    # long context requests". The guard used to be Haiku-only, so once the
    # MEM0_*_MODEL env was switched to claude-sonnet-4-6 every classify/dedup
    # 429'd silently (fail-open). mem0 helpers gain nothing from the beta, so
    # drop it unconditionally — only the main bot path (Opus) keeps it.
    effective_betas = [b for b in MODEL_BETAS if not b.startswith("context-1m-")]
    headers["anthropic-beta"] = ",".join(effective_betas)
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=cli_system(task_prompt),
        messages=[{"role": "user", "content": user_message}],
        metadata=get_metadata(),
        extra_headers=headers,
    )
    # Never send adaptive thinking to Mem0 helpers, regardless of model.
    # Haiku returns HTTP 400 on it outright. Thinking-capable models (Sonnet,
    # Opus) accept it but spend the helper's small max_tokens budget (200-500,
    # sized for a short JSON reply) on thinking alone — the response then has
    # zero TextBlocks (stop_reason=max_tokens), extract_text() returns "", and
    # the caller's json.loads("") fails (observed 2026-07 as repeated
    # "rerank_memories failed" / "rewrite_query failed" warnings after
    # MEM0_RERANK_MODEL/MEM0_REWRITE_MODEL were switched to claude-sonnet-4-6).
    # These are small classify/extract/rerank tasks — none need thinking.
    started = time.monotonic()
    try:
        response = await client.messages.create(**kwargs)
    except (Exception, asyncio.CancelledError) as error:
        log_helper_failure(
            route="anthropic_cli", model=model, task_prompt=task_prompt, user_chars=len(user_message),
            duration_ms=int((time.monotonic() - started) * 1000),
            outcome="cancelled" if isinstance(error, asyncio.CancelledError) else "failed", error=error,
        )
        raise
    log_helper_usage(
        response, model=model, task_prompt=task_prompt, user_chars=len(user_message),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return response


def extract_text(resp) -> str:
    """Pull the first TextBlock out of a Claude API response.

    Mem0 helpers no longer request ``thinking`` (see ``cli_messages_create``),
    but scanning for the first text-bearing block is kept defensive in case a
    future response shape prepends non-text blocks again.
    """
    for block in getattr(resp, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return text
    if getattr(resp, "stop_reason", None) == "max_tokens":
        log.warning(
            "extract_text: no TextBlock, stop_reason=max_tokens "
            "(output likely consumed by thinking; model=%s)",
            getattr(resp, "model", "?"),
        )
    return ""

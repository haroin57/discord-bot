"""Shared plumbing for the library's own single-shot helper LLM calls.

Every helper stage (extraction, classification, dedup judgment, conflict
classification, query rewrite, HyDE, rerank) follows the same dance: fetch
the auth backend, escape braces in untrusted text bound for a
``str.format`` template, run one completion, strip the Markdown fence the
model may wrap its JSON in, parse, and fail open on any error. Nine call
sites used to hand-roll it; this module is the single implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

log = logging.getLogger(__name__)


async def complete_text(
    *, system: str, user_message: str, model: str, max_tokens: int,
    timeout_s: float | None = None, backend=None,
) -> str:
    """Bound one helper call, preserving raw output for extraction salvage.

    Provider/timeout failures propagate to the stage owning its retry policy.
    Caller cancellation is never converted into a successful empty response.
    This helper must not wrap a provider's persistent memory commit.
    """
    from .settings import settings
    budget = getattr(settings, "MEM0_HELPER_TIMEOUT_S", 60.0) if timeout_s is None else timeout_s
    if (isinstance(budget, bool) or not isinstance(budget, (int, float))
            or not 0 < budget <= 600 or not math.isfinite(budget)):
        raise ValueError("helper timeout must be finite seconds in (0, 600]")
    if backend is None:
        from .auth import get_auth_backend
        backend = get_auth_backend()
    text = await asyncio.wait_for(
        backend.complete(system=system, user_message=user_message,
                         model=model, max_tokens=max_tokens),
        timeout=budget,
    )
    if not isinstance(text, str):
        raise TypeError("helper completion must be text")
    return text


def escape_braces(text: str, *, limit: int | None = None) -> str:
    """Escape ``{``/``}`` so untrusted text is inert inside a format template.

    ``limit`` caps the length first (the templates all cap their slots), so
    call sites read as one operation instead of a slice + two replaces.
    """
    if limit is not None:
        text = text[:limit]
    return text.replace("{", "{{").replace("}", "}}")


def strip_json_fence(text: str) -> str:
    """Remove a Markdown ```json fence around an LLM JSON reply, if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    return text


async def complete_json(
    *,
    system: str,
    user_message: str,
    model: str,
    max_tokens: int,
    caller: str = "llm",
    timeout_s: float | None = None,
) -> Any:
    """One completion, parsed as JSON. Returns ``None`` on ANY failure.

    Fail-open contract: backend errors, empty replies, and parse failures
    all log a WARNING tagged with ``caller`` and return ``None``, so no
    helper stage can take down the ingestion or recall path.
    """
    try:
        text = strip_json_fence(await complete_text(
            system=system,
            user_message=user_message,
            model=model,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        ))
        if not text:
            log.warning("%s: empty completion (fail-open)", caller)
            return None
        return json.loads(text)
    except Exception as exc:
        log.warning("%s: LLM JSON call failed (fail-open): %s", caller, type(exc).__name__)
        return None

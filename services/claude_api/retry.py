"""Per-channel retry budget + exponential backoff.

Ported from services/claude_sdk.py:110-120 + 502-522. The eviction strategy
is the master-branch FIFO behavior; a later commit on
security/phase2-unification (SRE4) replaces it with true LRU — we keep parity
with master here and will sync when that branch is merged.
"""

import random
import time

from config import (
    CLAUDE_MAX_ATTEMPTS,
    CLAUDE_MAX_BACKOFF_S,
    CLAUDE_TRANSIENT_MAX_ATTEMPTS,
)

# Per-channel retry budget — at most 3 transient retries per 60 s window.
# Bounded to 500 entries; oldest entry evicted FIFO when the limit is reached.
_retry_budget: dict[int, list[float]] = {}
_RETRY_BUDGET_MAX_CHANNELS = 500
_RETRY_BUDGET_WINDOW_S = 60
_RETRY_BUDGET_PER_WINDOW = 3

BASE_BACKOFF_S = 3
MAX_BACKOFF_S = CLAUDE_MAX_BACKOFF_S
DEFAULT_MAX_ATTEMPTS = CLAUDE_MAX_ATTEMPTS
TRANSIENT_MAX_ATTEMPTS = CLAUDE_TRANSIENT_MAX_ATTEMPTS


def check_and_record_retry_budget(channel_id: int) -> bool:
    """Return True if retry permitted; False if budget exhausted for this channel.

    Side effect: records a retry timestamp for the channel and evicts the oldest
    channel entry when over capacity.
    """
    now = time.monotonic()
    recent = [
        t for t in _retry_budget.get(channel_id, [])
        if now - t < _RETRY_BUDGET_WINDOW_S
    ]
    if len(recent) >= _RETRY_BUDGET_PER_WINDOW:
        return False
    recent.append(now)
    _retry_budget[channel_id] = recent
    if len(_retry_budget) > _RETRY_BUDGET_MAX_CHANNELS:
        oldest_key = next(iter(_retry_budget))
        del _retry_budget[oldest_key]
    return True


def retry_after_from_headers(headers) -> float | None:
    """Server-requested wait in seconds from a ``retry-after`` header, or None.

    Accepts any mapping (httpx.Headers / dict); both the delta-seconds and
    HTTP-date forms are handled. Used by entry.py so the backoff sleep honors
    the server's request instead of hammering the same wall every ~3-6s
    (audit #5 — the CLI computes max(retryAfter*1000, computedBackoff)).
    """
    if headers is None:
        return None
    value = None
    try:
        for k, v in headers.items():
            if str(k).lower() == "retry-after":
                value = str(v)
                break
    except AttributeError:
        return None
    if not value:
        return None
    from services.claude_api.ratelimit import parse_retry_after
    return parse_retry_after(value)


def compute_backoff(attempt: int, *, jitter: bool = True) -> float:
    """Exponential backoff: BASE * 2**attempt, capped at MAX_BACKOFF_S.

    With ``jitter=True`` (default) an *equal jitter* scheme is applied:
    half the capped delay is fixed and half is uniformly random in
    ``[0, capped/2]``. This breaks up synchronized retries so repeated
    overloaded/rate-limit failures don't all re-enter at the same instant
    (thundering herd) and keep colliding with the server's recovery window.

    ``jitter=False`` returns the deterministic capped delay — used by tests
    asserting exact values and by any caller that needs reproducibility.
    """
    capped = min(BASE_BACKOFF_S * (2 ** attempt), MAX_BACKOFF_S)
    if not jitter:
        return capped
    half = capped / 2
    return half + random.uniform(0, half)

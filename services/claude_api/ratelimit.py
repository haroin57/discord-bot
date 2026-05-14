"""Subscriber-tier rate-limit pacing.

The Anthropic API attaches ``anthropic-ratelimit-unified-*`` headers on
every successful response. Reading them and pacing future requests
proactively is how the real claude-cli avoids the 429-loop our naive
SDK retry falls into.

Headers we observe and use::

    anthropic-ratelimit-unified-status              allowed | allowed_warning | rejected
    anthropic-ratelimit-unified-5h-status           allowed | allowed_warning | rejected
    anthropic-ratelimit-unified-5h-utilization      float 0..1
    anthropic-ratelimit-unified-5h-reset            unix seconds
    anthropic-ratelimit-unified-7d-status           ...
    anthropic-ratelimit-unified-7d-utilization      float 0..1
    anthropic-ratelimit-unified-7d-reset            unix seconds
    anthropic-ratelimit-unified-7d-surpassed-threshold  float (e.g. 0.75)
    anthropic-ratelimit-unified-fallback-percentage     float 0..1 (probabilistic throttle)
    anthropic-ratelimit-unified-representative-claim    five_hour | seven_day | overage

When ``fallback-percentage > 0`` we have observed probabilistic 429s
even though utilization is < 1.0; pace by sleeping at least
``min_gap_s`` between requests to give the throttle window room.
"""

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Minimum gap between requests once we are in any kind of warning state.
# Keeps the bot from machine-gunning the API and tripping the soft throttle.
_GAP_WHEN_WARNING_S = 3.0
_GAP_WHEN_HARD_S = 10.0


@dataclass
class RateLimitState:
    status: str = "allowed"
    representative: str = ""
    five_h_status: str = "allowed"
    five_h_utilization: float = 0.0
    five_h_reset: int = 0
    seven_d_status: str = "allowed"
    seven_d_utilization: float = 0.0
    seven_d_reset: int = 0
    seven_d_surpassed: float = 0.0
    fallback_percentage: float = 0.0
    last_observed: float = 0.0
    last_request_at: float = 0.0


_state = RateLimitState()


def _float_or(headers, name: str, default: float = 0.0) -> float:
    v = headers.get(name)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int_or(headers, name: str, default: int = 0) -> int:
    v = headers.get(name)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _str_or(headers, name: str, default: str = "") -> str:
    v = headers.get(name)
    return v if isinstance(v, str) else default


def observe(headers) -> RateLimitState:
    """Update the shared RateLimitState from a response's headers.

    ``headers`` may be a plain dict, an ``httpx.Headers``, or any mapping;
    treat case-insensitively because httpx already lower-cases keys but
    other transports do not.
    """
    norm = {}
    try:
        for k, v in headers.items():
            norm[k.lower()] = v
    except AttributeError:
        return _state

    _state.status = _str_or(norm, "anthropic-ratelimit-unified-status", _state.status)
    _state.representative = _str_or(
        norm, "anthropic-ratelimit-unified-representative-claim", _state.representative,
    )
    _state.five_h_status = _str_or(
        norm, "anthropic-ratelimit-unified-5h-status", _state.five_h_status,
    )
    _state.five_h_utilization = _float_or(
        norm, "anthropic-ratelimit-unified-5h-utilization", _state.five_h_utilization,
    )
    _state.five_h_reset = _int_or(
        norm, "anthropic-ratelimit-unified-5h-reset", _state.five_h_reset,
    )
    _state.seven_d_status = _str_or(
        norm, "anthropic-ratelimit-unified-7d-status", _state.seven_d_status,
    )
    _state.seven_d_utilization = _float_or(
        norm, "anthropic-ratelimit-unified-7d-utilization", _state.seven_d_utilization,
    )
    _state.seven_d_reset = _int_or(
        norm, "anthropic-ratelimit-unified-7d-reset", _state.seven_d_reset,
    )
    _state.seven_d_surpassed = _float_or(
        norm, "anthropic-ratelimit-unified-7d-surpassed-threshold",
        _state.seven_d_surpassed,
    )
    _state.fallback_percentage = _float_or(
        norm, "anthropic-ratelimit-unified-fallback-percentage",
        _state.fallback_percentage,
    )
    _state.last_observed = time.time()
    log.info(
        "rate-limit observed: status=%s claim=%s 5h_util=%.2f 7d_util=%.2f "
        "fallback=%.2f reset_5h=%d reset_7d=%d",
        _state.status, _state.representative,
        _state.five_h_utilization, _state.seven_d_utilization,
        _state.fallback_percentage, _state.five_h_reset, _state.seven_d_reset,
    )
    return _state


def get_state() -> RateLimitState:
    return _state


def should_block() -> tuple[bool, str]:
    """Return ``(True, reason)`` if a new request must be refused outright.

    Conservative: only refuse when the probabilistic throttle is essentially
    100%. Subscription-bucket ``rejected`` and ``utilization >= 1.0`` do NOT
    block — claude.ai Extra Usage is server-side auto-switch, so when the
    subscription window is full the server itself decides whether to bill
    via the overage path (returning 200) or actually 429 us. Pre-emptively
    refusing here would prevent the overage path from ever being exercised
    (observed 2026-05-14: status=rejected but the live CLI keeps getting
    200 OK + overage-in-use=true).
    """
    if _state.fallback_percentage >= 0.99:
        return True, "API throttle active (fallback=100%)"
    return False, ""


async def maybe_wait_before_request() -> None:
    """Sleep just enough to honor the soft throttle.

    No-op when no warnings are observed yet. Once `status=allowed_warning`
    is seen, enforces a minimum gap between consecutive calls; the gap is
    longer when the fallback-percentage indicates active throttling.
    """
    import asyncio

    now = time.time()
    elapsed = now - (_state.last_request_at or 0.0)
    gap = 0.0
    if _state.fallback_percentage > 0:
        gap = _GAP_WHEN_HARD_S
    elif _state.status in ("allowed_warning",):
        gap = _GAP_WHEN_WARNING_S
    elif _state.seven_d_utilization >= _state.seven_d_surpassed > 0:
        gap = _GAP_WHEN_WARNING_S

    if gap > 0 and elapsed < gap:
        wait = gap - elapsed
        log.info("pacing: sleeping %.1fs (status=%s fallback=%.2f)",
                 wait, _state.status, _state.fallback_percentage)
        await asyncio.sleep(wait)
    _state.last_request_at = time.time()

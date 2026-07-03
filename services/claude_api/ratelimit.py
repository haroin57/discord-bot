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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

_LIMIT_METRICS = (
    ("リクエスト", "anthropic-ratelimit-requests"),
    ("総トークン", "anthropic-ratelimit-tokens"),
    ("入力トークン", "anthropic-ratelimit-input-tokens"),
    ("出力トークン", "anthropic-ratelimit-output-tokens"),
    ("Priority入力", "anthropic-priority-input-tokens"),
    ("Priority出力", "anthropic-priority-output-tokens"),
)

_UNIFIED_RESETS = (
    ("5時間枠", "anthropic-ratelimit-unified-5h-reset"),
    ("7日枠", "anthropic-ratelimit-unified-7d-reset"),
)


try:
    _DISPLAY_TZ = ZoneInfo("Asia/Tokyo")
except ZoneInfoNotFoundError:
    _DISPLAY_TZ = timezone.utc


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


def _normalize_headers(headers) -> dict[str, str]:
    norm: dict[str, str] = {}
    try:
        items = headers.items()
    except AttributeError:
        return norm
    for key, value in items:
        norm[str(key).lower()] = str(value)
    return norm


def _optional_int(headers: dict[str, str], name: str) -> int | None:
    value = headers.get(name)
    if value is None or value == "":
        return None
    try:
        return int(float(value.replace(",", "")))
    except (TypeError, ValueError):
        return None


def _optional_float(headers: dict[str, str], name: str) -> float | None:
    value = headers.get(name)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_unix_seconds(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def parse_retry_after(value: "str | None", *, now: "datetime | None" = None) -> "float | None":
    """Parse a retry-after value (delta-seconds or HTTP-date) into seconds.

    Single source of truth for retry-after parsing (P1-a): the pacing
    formatter here and the backoff logic in retry.py previously carried two
    divergent copies of this exact dance.
    """
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now if now is not None else datetime.now(timezone.utc)
    return max(0.0, (parsed - reference).total_seconds())


def _retry_after_seconds(value: str | None, now: datetime) -> int | None:
    parsed = parse_retry_after(value, now=now)
    return None if parsed is None else int(parsed)


def _format_dt(value: datetime) -> str:
    return value.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_wait(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{sec}秒" if sec else f"{minutes}分"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}時間{minutes}分" if minutes else f"{hours}時間"
    days, hours = divmod(hours, 24)
    return f"{days}日{hours}時間" if hours else f"{days}日"


def _format_usage(label: str, limit: int | None, remaining: int | None) -> str | None:
    if limit is None and remaining is None:
        return None
    if limit is None:
        return f"{label}: 残り {remaining:,}"
    if remaining is None:
        return f"{label}: 上限 {limit:,}"
    used = max(limit - remaining, 0)
    pct = (used / limit * 100.0) if limit > 0 else 0.0
    return f"{label}: {used:,}/{limit:,} 使用（残り {remaining:,}、{pct:.0f}%）"


def _reset_candidates(norm: dict[str, str], now: datetime) -> list[datetime]:
    candidates: list[datetime] = []
    retry_after = _retry_after_seconds(norm.get("retry-after"), now)
    if retry_after is not None:
        candidates.append(now + timedelta(seconds=retry_after))
    for _, prefix in _LIMIT_METRICS:
        parsed = _parse_rfc3339(norm.get(f"{prefix}-reset"))
        if parsed is not None:
            candidates.append(parsed)
    for _, name in _UNIFIED_RESETS:
        parsed = _parse_unix_seconds(norm.get(name))
        if parsed is not None:
            candidates.append(parsed)
    return [candidate for candidate in candidates if candidate >= now]


def format_rate_limit_details(headers, *, now: datetime | None = None) -> str:
    """Return user-facing details extracted from Anthropic rate-limit headers."""
    norm = _normalize_headers(headers)
    if not norm:
        return ""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    lines: list[str] = []
    retry_after = _retry_after_seconds(norm.get("retry-after"), now)
    candidates = _reset_candidates(norm, now)
    if retry_after is not None:
        reset_at = now + timedelta(seconds=retry_after)
        lines.append(
            f"解除目安: {_format_dt(reset_at)}（約{_format_wait(retry_after)}後）"
        )
    elif candidates:
        reset_at = min(candidates)
        wait_seconds = max(0, int((reset_at - now).total_seconds()))
        lines.append(
            f"解除目安: {_format_dt(reset_at)}（約{_format_wait(wait_seconds)}後）"
        )

    usage_parts: list[str] = []
    for label, prefix in _LIMIT_METRICS:
        part = _format_usage(
            label,
            _optional_int(norm, f"{prefix}-limit"),
            _optional_int(norm, f"{prefix}-remaining"),
        )
        if part:
            usage_parts.append(part)
    if usage_parts:
        lines.append("使用状況: " + " / ".join(usage_parts))

    unified_parts: list[str] = []
    five_h = _optional_float(norm, "anthropic-ratelimit-unified-5h-utilization")
    seven_d = _optional_float(norm, "anthropic-ratelimit-unified-7d-utilization")
    fallback = _optional_float(norm, "anthropic-ratelimit-unified-fallback-percentage")
    representative = norm.get("anthropic-ratelimit-unified-representative-claim")
    if representative:
        unified_parts.append(f"代表枠 {representative}")
    if five_h is not None:
        unified_parts.append(f"5時間枠 {five_h * 100:.0f}%")
    if seven_d is not None:
        unified_parts.append(f"7日枠 {seven_d * 100:.0f}%")
    if fallback is not None:
        unified_parts.append(f"fallback {fallback * 100:.0f}%")
    if unified_parts:
        lines.append("購読枠: " + " / ".join(unified_parts))

    return "\n".join(lines)


def append_rate_limit_details(message: str, headers) -> str:
    details = format_rate_limit_details(headers)
    if not details:
        return message
    return f"{message}\n{details}"


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


def reset_state() -> None:
    """観測状態を初期化する（OAuth アカウント切替時に呼ぶ）。

    切替後の観測はすべて新アカウントのものなので、旧アカウントの
    utilization / reset 時刻を持ち越すとブロック判定・ペーシングが
    アカウント混在で誤動作する。全参照はモジュール名 `_state` 経由なので
    再束縛で安全（get_state() の返した参照を長期保持する呼び元は無い）。
    """
    global _state
    _state = RateLimitState()


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

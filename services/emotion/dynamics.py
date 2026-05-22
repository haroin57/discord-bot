"""Emotion dynamics v2 — anti-flatness layer for the LLM emotion engine.

All functions here are pure (no I/O). They live behind
``params.EMOTION_DYNAMICS_V2`` and are consumed by:

  - ``llm_transition._build_prompt`` (variance / circadian / mood injection)
  - ``validator.validate_and_apply`` (post-clamp perturbation + label reset)
  - ``timeseries.append_timeseries`` (drasticness telemetry)

Design notes:
  - mood label + axis_bias are date-only (deterministic per day) so
    "今日はざわつく日" stays coherent across all ticks in that day.
  - tick-level micro jitter is decoupled (TICK_JITTER_AMPLITUDE) to keep
    same-tick reproducibility without making the day-color flicker.
  - mean-reversion runs in silence mode only; conversation ticks must
    leave LLM's semantic moves intact.
  - perturb returns the set of axes it moved beyond LABEL_RESET_THRESHOLD
    so the caller can clear the corresponding `state.labels[axis]` to
    keep the v3 invariant "displayed label never disagrees with numeric"
    (see services/emotion/format.py:1-12).
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from services.emotion.baseline import load_baseline
from services.emotion.params import (
    BOUNDS,
    CIRCADIAN_ANXIETY_NIGHT_BUMP,
    CIRCADIAN_AROUSAL_AMPLITUDE,
    CIRCADIAN_AROUSAL_PEAK_HOUR,
    CIRCADIAN_PULL_ANXIETY,
    CIRCADIAN_PULL_AROUSAL,
    CIRCADIAN_PULL_AROUSAL_NIGHT,
    FLAT_NOISE_MULTIPLIER,
    FLAT_STD_THRESHOLD,
    LABEL_RESET_THRESHOLD,
    MEAN_REVERSION_STRENGTH,
    MOOD_BIAS_AMPLITUDE,
    NOISE_SIGMA_CONVERSATION,
    NOISE_SIGMA_SILENCE,
    TICK_JITTER_AMPLITUDE,
)

if TYPE_CHECKING:
    from services.emotion.state import EmotionState

NUMERIC_FIELDS = (
    "valence", "arousal", "loneliness", "affection", "curiosity", "anxiety",
)

# Journal entries use short keys (v/ar/lo/af/cu/ax). Map to full axis names.
_AXIS_SHORT = {
    "valence": "v", "arousal": "ar", "loneliness": "lo",
    "affection": "af", "curiosity": "cu", "anxiety": "ax",
}

MOOD_LABELS_POOL: tuple[str, ...] = (
    "穏やかな日", "ざわつく日", "重い日", "明るすぎる日", "やや厭世的な日",
    "甘えたい日", "刺々しい日", "凪な日", "感傷的な日", "妙にハイな日",
    "退屈な日", "ノスタルジックな日",
)


def rolling_stats(journal: list[dict], n: int = 25) -> dict[str, dict]:
    """Compute mean/std/min/max for last n journal entries.

    Returns dict keyed by full axis name. Empty / single-entry journal
    yields zeros (std needs ≥2 samples; pstdev handles 1 sample as 0).
    """
    entries = journal[-n:] if journal else []
    out: dict[str, dict] = {}
    for axis, short in _AXIS_SHORT.items():
        vals = [e[short] for e in entries if short in e]
        if not vals:
            out[axis] = {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
        elif len(vals) == 1:
            out[axis] = {"mean": vals[0], "std": 0.0, "min": vals[0], "max": vals[0]}
        else:
            out[axis] = {
                "mean": statistics.mean(vals),
                "std": statistics.pstdev(vals),
                "min": min(vals),
                "max": max(vals),
            }
    return out


def circadian_baseline(now: datetime) -> dict[str, float]:
    """Physical baselines for arousal (cosine) + anxiety (night triangle).

    arousal: 0.5 + amp * cos((h - peak) * π / 12)
      → trough at peak±12h, peak at the named hour
      defaults: amp=0.30, peak=14 → 04:00 = 0.20, 14:00 = 0.80
    anxiety: 0.30 baseline, +CIRCADIAN_ANXIETY_NIGHT_BUMP at 03:30
             (linear triangle 02:00→03:30→05:00)
    """
    h = now.hour + now.minute / 60.0
    arousal = 0.5 + CIRCADIAN_AROUSAL_AMPLITUDE * math.cos(
        (h - CIRCADIAN_AROUSAL_PEAK_HOUR) * math.pi / 12
    )
    if 2 <= h < 5:
        t = (h - 2) / 3.0  # 0..1
        anxiety = 0.30 + CIRCADIAN_ANXIETY_NIGHT_BUMP * (1 - abs(2 * t - 1))
    else:
        anxiety = 0.30
    return {"arousal": arousal, "anxiety": anxiety}


@dataclass(frozen=True)
class DailyMood:
    label: str
    axis_bias: dict[str, float]
    date_key: str


def daily_mood_seed(now: datetime) -> DailyMood:
    """Date-only seed. Same day → same label + axis_bias.

    Uses sha1(YYYY-MM-DD). 6 bytes for 6 axis biases, mapped to
    uniform[-MOOD_BIAS_AMPLITUDE, +MOOD_BIAS_AMPLITUDE].
    """
    date_key = now.strftime("%Y-%m-%d")
    h = hashlib.sha1(date_key.encode()).digest()
    label = MOOD_LABELS_POOL[h[0] % len(MOOD_LABELS_POOL)]
    axis_bias = {
        axis: (h[i + 1] / 255.0 * 2 - 1) * MOOD_BIAS_AMPLITUDE
        for i, axis in enumerate(NUMERIC_FIELDS)
    }
    return DailyMood(label=label, axis_bias=axis_bias, date_key=date_key)


def tick_jitter(now: datetime) -> dict[str, float]:
    """Per-tick micro jitter (±TICK_JITTER_AMPLITUDE)."""
    tick_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
    tick_key = f"{now.strftime('%Y-%m-%d')}-{tick_idx}"
    h = hashlib.sha1(tick_key.encode()).digest()
    return {
        axis: (h[i] / 255.0 * 2 - 1) * TICK_JITTER_AMPLITUDE
        for i, axis in enumerate(NUMERIC_FIELDS)
    }


def max_axis_jump(prev, new: dict) -> float:
    """Max absolute delta across 6 axes. `prev` may be EmotionState or dict."""
    def _get(obj, axis: str) -> float:
        if isinstance(obj, dict):
            return float(obj[axis])
        return float(getattr(obj, axis))
    return max(abs(_get(new, k) - _get(prev, k)) for k in NUMERIC_FIELDS)


def perturb(
    numerics: dict,
    *,
    prev_state,  # EmotionState | dict (unused here, kept for future use)
    journal: list[dict],
    now: datetime,
    mode: str,
    rng: random.Random | None = None,
) -> tuple[dict, set[str]]:
    """Apply Gaussian noise + circadian pull + (silence-only) mean-reversion.

    Returns (new_numerics, perturbed_axes) where perturbed_axes is the
    set of axis names that moved by >= LABEL_RESET_THRESHOLD. Caller
    should clear `state.labels[axis]` for those axes (see validator.py).

    Mood bias is intentionally NOT applied here — it's prompt-only, so
    LLM can choose to follow or override it semantically. Applying it
    algorithmically would cause day-long directional accumulation.
    """
    del prev_state  # reserved
    if rng is None:
        rng = random.Random()

    sigma_map = NOISE_SIGMA_SILENCE if mode == "silence" else NOISE_SIGMA_CONVERSATION
    jitter = tick_jitter(now)
    cb = circadian_baseline(now)
    stats = rolling_stats(journal)
    hour = now.hour

    pull_arousal = CIRCADIAN_PULL_AROUSAL
    if 2 <= hour < 6 and mode == "silence":
        pull_arousal = CIRCADIAN_PULL_AROUSAL_NIGHT

    # LLM-derived rest anchor (silence-only). None when cache is missing /
    # corrupt — in that case mean-reversion is skipped entirely.
    baseline = load_baseline() if mode == "silence" else None

    perturbed_axes: set[str] = set()
    out = dict(numerics)
    for axis in NUMERIC_FIELDS:
        sigma = sigma_map[axis]
        if stats[axis]["std"] < FLAT_STD_THRESHOLD:
            sigma *= FLAT_NOISE_MULTIPLIER
        # zero-mean noise + tick jitter
        mu = jitter[axis]
        delta = rng.gauss(mu, sigma)
        # circadian pull (arousal/anxiety only)
        if axis == "arousal":
            delta += pull_arousal * (cb["arousal"] - out[axis])
        elif axis == "anxiety":
            delta += CIRCADIAN_PULL_ANXIETY * (cb["anxiety"] - out[axis])
        # mean-reversion toward baseline — silence only.
        # Conversation ticks must preserve LLM's semantic moves.
        if baseline is not None:
            delta += MEAN_REVERSION_STRENGTH[axis] * (
                baseline[axis] - out[axis]
            )
        lo, hi = BOUNDS[axis]
        new_val = max(lo, min(hi, out[axis] + delta))
        if abs(new_val - out[axis]) >= LABEL_RESET_THRESHOLD:
            perturbed_axes.add(axis)
        out[axis] = new_val
    return out, perturbed_axes

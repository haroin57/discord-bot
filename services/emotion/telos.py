"""Telos generator — derives a contact motivation from the current state.

The whole reason this architecture exists: かなえ shouldn't message 先輩
because cron fired. She messages because she has a reason to — loneliness
crossed a threshold, something good happened she wants to share, she's
worried, etc. If no motivation is generated, no message is sent.

`generate_telos` is pure (no IO). `should_suppress` rolls dice for
personality-based hold-back (ツンデレ抑制 etc.) and takes an injectable
`rng` so tests can be deterministic.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone

from services.emotion.params import (
    LATE_NIGHT_HOUR_START,
    MAX_CONSECUTIVE_SELF_INITIATED,
    TELOS_THRESHOLDS,
    TSUNDERE_GATE,
)
from services.emotion.state import EmotionState

T = TELOS_THRESHOLDS


@dataclass
class Motivation:
    type: str             # loneliness_overflow / share_joy / anxiety_relief / ...
    urgency: float        # 0..1; ties get broken by insertion order
    expression_hint: str  # short Japanese phrase the prompt embeds verbatim


def _hours_since(iso_ts: str | None, now: datetime) -> float:
    """Return hours between `iso_ts` and `now`, or +inf if unknown."""
    if not iso_ts:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return float("inf")
    if dt.tzinfo is None:
        # Assume local time for naive stamps so we don't accidentally
        # interpret them as UTC and produce a 9-hour skew.
        dt = dt.replace(tzinfo=now.tzinfo or timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def generate_telos(state: EmotionState, now: datetime) -> Motivation | None:
    """Pick the strongest motivation, or None to stay silent."""
    motivations: list[Motivation] = []
    hrs_silent = _hours_since(state.last_interaction.timestamp, now)

    if state.loneliness > T["loneliness_overflow"]:
        motivations.append(Motivation(
            "loneliness_overflow", state.loneliness,
            "寂しさを直接は言わないが、なんでもない話題で連絡する",
        ))
    if state.valence > T["share_joy_valence"] and state.arousal > T["share_joy_arousal"]:
        motivations.append(Motivation(
            "share_joy", state.arousal,
            "嬉しかった出来事を報告したい",
        ))
    if state.anxiety > T["anxiety_relief_anxiety"] and hrs_silent > T["anxiety_relief_hours_silent"]:
        motivations.append(Motivation(
            "anxiety_relief", state.anxiety,
            "先輩が元気か確認したい（直接は聞かない）",
        ))
    if (state.affection > T["affection_expression_affection"]
            and state.valence > T["affection_expression_valence"]):
        motivations.append(Motivation(
            "affection_expression", 0.4,
            "甘えたい、構ってほしい",
        ))
    if state.curiosity > T["curiosity_drive"]:
        motivations.append(Motivation(
            "topic_driven", state.curiosity * 0.7,
            "聞きたいこと・話したいことがある",
        ))
    if (state.valence < T["reconciliation_valence_neg"]
            and state.affection > T["reconciliation_affection"]):
        motivations.append(Motivation(
            "reconciliation",
            abs(state.valence) * state.affection,
            "気まずさを解消したい",
        ))

    if not motivations:
        return None

    # Highest urgency wins; deterministic tie-breaking by first-appended.
    motivations.sort(key=lambda m: m.urgency, reverse=True)
    return motivations[0]


def should_suppress(
    state: EmotionState,
    telos: Motivation,
    now: datetime,
    rng: random.Random | None = None,
) -> bool:
    """Personality-driven hold-back. Pure given `rng`.

    Three guards:
      - **ツンデレ抑制** — high affection paired with low expression
        comfort means she sometimes refuses to say it out loud.
      - **プライド抑制** — after 2 consecutive self-initiated pings, she
        backs off rather than look needy.
      - **深夜抑制** — late evening (≥22 JST) with weak urgency means she
        waits until morning. Stronger urgencies (>0.8) override this.
    """
    rng = rng or random

    if telos.type == "affection_expression" and rng.random() < TSUNDERE_GATE * 0.6:
        return True

    if (state.consecutive_self_initiated >= MAX_CONSECUTIVE_SELF_INITIATED
            and rng.random() < 0.7):
        return True

    if now.hour >= LATE_NIGHT_HOUR_START and telos.urgency < 0.8 and rng.random() < 0.6:
        return True

    return False

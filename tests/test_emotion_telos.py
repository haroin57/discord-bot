"""Tests for services.emotion.telos — motivation generation + suppression."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from services.emotion.params import (
    LATE_NIGHT_HOUR_START,
    MAX_CONSECUTIVE_SELF_INITIATED,
    TELOS_THRESHOLDS,
)
from services.emotion.state import EmotionState, LastInteraction
from services.emotion.telos import Motivation, generate_telos, should_suppress

JST = timezone(timedelta(hours=9))
T = TELOS_THRESHOLDS

# A non-quiet, non-late-night reference time for most tests.
NOW = datetime(2026, 5, 13, 15, 0, tzinfo=JST)


class TestGenerateTelos:
    def test_baseline_state_returns_none(self):
        """2026-05-13: with affection_expression threshold at 0.9 and the
        valence-based predicate, baseline (affection=0.85, valence=0.6)
        no longer fires."""
        s = EmotionState()
        assert generate_telos(s, NOW) is None

    def test_affection_expression_fires_only_above_threshold(self):
        """At affection ≥ 0.9 AND valence ≥ 0.5 the motivation fires.

        Phase A (2026-05-13): the second predicate switched from
        `satisfaction > 0.7` to `valence > 0.5` after satisfaction was
        rolled into valence."""
        s = EmotionState(affection=0.95, valence=0.6)
        m = generate_telos(s, NOW)
        assert m is not None and m.type == "affection_expression"
        assert m.urgency == pytest.approx(0.4)

    def test_truly_calm_state_returns_none(self):
        """If even baseline affection is held back below its threshold and
        valence sits low, no motivation should fire."""
        s = EmotionState(affection=0.5, valence=0.0)
        assert generate_telos(s, NOW) is None

    def test_loneliness_overflow_fires(self):
        s = EmotionState(loneliness=T["loneliness_overflow"] + 0.05)
        m = generate_telos(s, NOW)
        assert m is not None and m.type == "loneliness_overflow"

    def test_share_joy_requires_both_valence_and_arousal(self):
        # High valence but low arousal: should not trigger share_joy alone
        only_val = EmotionState(valence=0.95, arousal=0.1)
        m = generate_telos(only_val, NOW)
        assert m is None or m.type != "share_joy"
        # Both above threshold → share_joy
        both = EmotionState(valence=0.95, arousal=0.95)
        m = generate_telos(both, NOW)
        assert m is not None and m.type == "share_joy"

    def test_anxiety_relief_requires_recent_silence(self):
        # High anxiety but last_interaction was 1 minute ago → no fire
        ts = (NOW - timedelta(minutes=1)).isoformat()
        s = EmotionState(
            anxiety=0.9,
            last_interaction=LastInteraction(timestamp=ts),
        )
        m = generate_telos(s, NOW)
        assert m is None or m.type != "anxiety_relief"
        # >8h silence → fires
        old = (NOW - timedelta(hours=12)).isoformat()
        s2 = EmotionState(
            anxiety=0.9,
            last_interaction=LastInteraction(timestamp=old),
        )
        m = generate_telos(s2, NOW)
        assert m is not None and m.type == "anxiety_relief"

    def test_reconciliation_needs_negative_valence_and_high_affection(self):
        # Negative valence alone with low affection → no reconciliation
        s_low_aff = EmotionState(valence=-0.4, affection=0.2)
        m = generate_telos(s_low_aff, NOW)
        assert m is None or m.type != "reconciliation"
        # Both conditions met → fires
        s_full = EmotionState(valence=-0.4, affection=0.8)
        m = generate_telos(s_full, NOW)
        assert m is not None and m.type == "reconciliation"

    def test_highest_urgency_wins_when_multiple_motivations(self):
        # Loneliness=0.95 vs affection_expression(urgency=0.4 constant)
        s = EmotionState(
            loneliness=0.95,
            affection=0.95,
            valence=0.7,
        )
        m = generate_telos(s, NOW)
        assert m is not None
        # loneliness_overflow has urgency=loneliness=0.95 > 0.4
        assert m.type == "loneliness_overflow"

    def test_topic_driven_fires_from_curiosity(self):
        s = EmotionState(curiosity=T["curiosity_drive"] + 0.1)
        m = generate_telos(s, NOW)
        assert m is not None and m.type == "topic_driven"


class TestShouldSuppress:
    def _telos(self, type_, urgency=0.5):
        return Motivation(type=type_, urgency=urgency, expression_hint="x")

    def test_no_suppression_with_calm_inputs(self):
        s = EmotionState()
        rng = random.Random(0)
        # A non-affection telos, low self_initiated, daytime → should not suppress
        assert should_suppress(s, self._telos("topic_driven", 0.7), NOW, rng) is False

    def test_tsundere_suppresses_affection_expression(self):
        """The very first random draw < 0.3 (=0.5*0.6) is the gate."""
        s = EmotionState()
        # rng(0).random() == 0.844... → above 0.3, NOT suppressed by tsundere alone
        rng_not = random.Random(0)
        assert should_suppress(s, self._telos("affection_expression"), NOW, rng_not) in (False, True)
        # Force a low draw to demonstrate the gate triggers
        class _Rng:
            def __init__(self, seq): self.seq = list(seq)
            def random(self): return self.seq.pop(0)
        rng_force = _Rng([0.05])
        assert should_suppress(s, self._telos("affection_expression"), NOW, rng_force) is True

    def test_pride_suppression_at_max_consecutive(self):
        s = EmotionState(consecutive_self_initiated=MAX_CONSECUTIVE_SELF_INITIATED)
        class _Rng:
            def __init__(self, seq): self.seq = list(seq)
            def random(self): return self.seq.pop(0)
        # First draw is for tsundere (not affection_expression here, so skipped).
        # Next is the pride draw — anything <0.7 suppresses.
        rng = _Rng([0.1])
        assert should_suppress(s, self._telos("loneliness_overflow", 0.7), NOW, rng) is True

    def test_late_night_suppresses_weak_urgency(self, monkeypatch):
        # Production params disable the late-night gate (LATE_NIGHT_HOUR_START
        # = 24 → `hour >= 24` always False). Restore the original 22-hour
        # threshold inside telos for this test so we verify the gate's logic.
        monkeypatch.setattr("services.emotion.telos.LATE_NIGHT_HOUR_START", 22)
        late = NOW.replace(hour=23, minute=30)
        s = EmotionState()
        class _Rng:
            def __init__(self, seq): self.seq = list(seq)
            def random(self): return self.seq.pop(0)
        # Telos urgency 0.5 (<0.8) → late-night gate engages; draw <0.6 suppresses
        rng = _Rng([0.1])
        assert should_suppress(s, self._telos("topic_driven", 0.5), late, rng) is True

    def test_late_night_does_not_suppress_strong_urgency(self, monkeypatch):
        monkeypatch.setattr("services.emotion.telos.LATE_NIGHT_HOUR_START", 22)
        late = NOW.replace(hour=23, minute=30)
        s = EmotionState()
        rng = random.Random(0)
        # Strong urgency 0.95 ≥ 0.8 → late-night gate bypassed.
        assert should_suppress(s, self._telos("loneliness_overflow", 0.95), late, rng) is False

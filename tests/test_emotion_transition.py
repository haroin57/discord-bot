"""Tests for services.emotion.transition — time decay + event blending."""

import random

import pytest

from services.emotion.params import (
    BASELINE,
    BOUNDS,
    EVENT_DELTAS,
    INERTIA,
    LONELINESS_RATE_PER_HOUR,
)
from services.emotion.state import EmotionState, LastInteraction
from services.emotion.transition import apply_event, apply_time_decay


class TestApplyTimeDecay:
    def test_zero_elapsed_is_no_op(self):
        s = EmotionState(loneliness=0.3, valence=0.5)
        out = apply_time_decay(s, 0.0)
        assert out.loneliness == s.loneliness
        assert out.valence == s.valence

    def test_loneliness_grows_linearly(self):
        s = EmotionState(loneliness=0.3)
        out = apply_time_decay(s, 6.0)
        # 0.3 + 0.03 * 6 = 0.48
        assert out.loneliness == pytest.approx(0.3 + LONELINESS_RATE_PER_HOUR * 6.0, abs=1e-6)

    def test_loneliness_capped_at_one(self):
        s = EmotionState(loneliness=0.95)
        out = apply_time_decay(s, 100.0)
        assert out.loneliness == pytest.approx(1.0)

    def test_valence_reverts_to_baseline_from_above(self):
        """A high valence should drift down toward BASELINE['valence']."""
        s = EmotionState(valence=0.95)
        out = apply_time_decay(s, 24.0)
        # Should not still be 0.95; should be measurably closer to 0.6
        assert out.valence < 0.95
        assert abs(out.valence - BASELINE["valence"]) < abs(0.95 - BASELINE["valence"])

    def test_valence_reverts_to_baseline_from_below(self):
        s = EmotionState(valence=-0.5)
        out = apply_time_decay(s, 24.0)
        assert out.valence > -0.5
        assert abs(out.valence - BASELINE["valence"]) < abs(-0.5 - BASELINE["valence"])

    def test_components_stay_within_bounds(self):
        # Force every component near its boundary; decay should never push out.
        s = EmotionState(
            valence=-1.0, arousal=1.0,
            loneliness=0.0, affection=1.0, curiosity=0.0,
            anxiety=0.0,
        )
        out = apply_time_decay(s, 5.0)
        for key, (lo, hi) in BOUNDS.items():
            v = getattr(out, key)
            assert lo - 1e-9 <= v <= hi + 1e-9, f"{key}={v} out of [{lo},{hi}]"

    def test_fractional_hour_still_advances_loneliness(self):
        s = EmotionState(loneliness=0.3)
        out = apply_time_decay(s, 0.5)
        # 0.3 + 0.03 * 0.5 = 0.315
        assert out.loneliness == pytest.approx(0.315, abs=1e-6)

    def test_good_recent_chat_slows_loneliness_growth(self):
        """Phase 2: high quality * responsiveness → loneliness rises slower."""
        good = EmotionState(
            loneliness=0.3,
            last_interaction=LastInteraction(
                timestamp="2026-05-13T20:00:00+09:00",
                quality=0.95, senpai_responsiveness=0.95,
            ),
        )
        bad = EmotionState(
            loneliness=0.3,
            last_interaction=LastInteraction(
                timestamp="2026-05-13T20:00:00+09:00",
                quality=0.1, senpai_responsiveness=0.1,
            ),
        )
        good_out = apply_time_decay(good, 6.0)
        bad_out = apply_time_decay(bad, 6.0)
        # Good chat → less loneliness growth than bad chat
        assert good_out.loneliness < bad_out.loneliness
        # Bad chat actually pushes growth above the raw α*h floor
        raw = 0.3 + LONELINESS_RATE_PER_HOUR * 6.0
        assert bad_out.loneliness > raw
        assert good_out.loneliness < raw

    def test_quality_modifier_no_op_when_no_last_interaction(self):
        """A fresh state (no last_interaction timestamp) must behave exactly
        like the pre-Phase-2 baseline so back-compat is preserved."""
        s = EmotionState(loneliness=0.3)
        out = apply_time_decay(s, 6.0)
        expected = 0.3 + LONELINESS_RATE_PER_HOUR * 6.0
        assert out.loneliness == pytest.approx(expected, abs=1e-6)


class TestApplyEvent:
    def test_unknown_event_is_no_op(self):
        s = EmotionState()
        out = apply_event(s, "this_event_does_not_exist")
        assert out == s

    def test_long_good_chat_lowers_loneliness(self):
        """Inertia γ=0.85 for loneliness → 15% of the delta should land."""
        s = EmotionState(loneliness=0.7)
        rng = random.Random(0)  # deterministic noise
        out = apply_event(s, "long_good_chat", intensity=1.0, rng=rng)
        # Expected: γ*0.7 + (1-γ)*(0.7 + -0.5) + noise ≈ 0.7 - 0.075 ± 0.05
        # We just assert directional + bound; exact value depends on rng seed.
        assert out.loneliness < s.loneliness
        assert out.loneliness >= 0.0

    def test_praised_raises_valence(self):
        s = EmotionState(valence=0.5)
        rng = random.Random(0)
        out = apply_event(s, "praised", intensity=1.0, rng=rng)
        # Phase A: satisfaction was folded into valence so "praised" lifts
        # valence by a larger amount than before (0.20 → 0.35 in the matrix).
        assert out.valence > s.valence

    def test_conflict_drops_valence_raises_arousal(self):
        s = EmotionState(valence=0.5, arousal=0.4, anxiety=0.1)
        rng = random.Random(0)
        out = apply_event(s, "conflict", intensity=1.0, rng=rng)
        assert out.valence < s.valence
        assert out.arousal > s.arousal
        assert out.anxiety > s.anxiety

    def test_intensity_scales_delta(self):
        s = EmotionState(valence=0.0)
        rng_a = random.Random(0)
        rng_b = random.Random(0)
        weak = apply_event(s, "praised", intensity=0.2, rng=rng_a)
        strong = apply_event(s, "praised", intensity=1.0, rng=rng_b)
        # Same rng seed → noise cancels out; strong shift > weak shift.
        assert abs(strong.valence - s.valence) > abs(weak.valence - s.valence)

    def test_bounds_respected_after_event(self):
        s = EmotionState(
            valence=0.95, arousal=0.95, loneliness=0.05,
            affection=0.95,
        )
        rng = random.Random(42)
        out = apply_event(s, "sweet_words", intensity=1.0, rng=rng)
        for key, (lo, hi) in BOUNDS.items():
            v = getattr(out, key)
            assert lo - 1e-9 <= v <= hi + 1e-9, f"{key}={v} out of [{lo},{hi}]"

    def test_all_event_types_in_matrix_are_runnable(self):
        s = EmotionState()
        rng = random.Random(123)
        for event_type in EVENT_DELTAS:
            out = apply_event(s, event_type, intensity=1.0, rng=rng)
            assert isinstance(out, EmotionState)

"""Tests for services.emotion.dynamics — pure functions for v2 anti-flatness."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from services.emotion.dynamics import (
    MOOD_LABELS_POOL,
    NUMERIC_FIELDS,
    DailyMood,
    circadian_baseline,
    daily_mood_seed,
    max_axis_jump,
    perturb,
    rolling_stats,
    tick_jitter,
)
from services.emotion.params import (
    BOUNDS,
    FLAT_NOISE_MULTIPLIER,
    FLAT_STD_THRESHOLD,
    LABEL_RESET_THRESHOLD,
    MOOD_BIAS_AMPLITUDE,
    NOISE_SIGMA_CONVERSATION,
    NOISE_SIGMA_SILENCE,
    TICK_JITTER_AMPLITUDE,
)

JST = timezone(timedelta(hours=9))

# Test fixture standing in for the (now-removed) hardcoded EMOTION_BASELINE.
# These are the legacy values, kept here so the silence-mode mean-reversion
# tests still pin a known anchor. Real baseline comes from
# services.emotion.baseline.load_baseline() at runtime.
_TEST_BASELINE = {
    "valence": 0.55, "arousal": 0.55, "loneliness": 0.35,
    "affection": 0.82, "curiosity": 0.55, "anxiety": 0.25,
}


@pytest.fixture(autouse=True)
def _patch_baseline(monkeypatch):
    """Inject a known baseline so dynamics tests have a stable rest anchor."""
    monkeypatch.setattr(
        "services.emotion.dynamics.load_baseline",
        lambda *a, **kw: dict(_TEST_BASELINE),
    )


def _make_journal(values: list[tuple[float, ...]]) -> list[dict]:
    """Build a journal from list of (v, ar, lo, af, cu, ax) tuples."""
    return [
        {"ts": "x", "v": v, "ar": ar, "lo": lo, "af": af, "cu": cu, "ax": ax}
        for v, ar, lo, af, cu, ax in values
    ]


def _flat_state() -> dict:
    return {axis: 0.5 for axis in NUMERIC_FIELDS}


# ---------------------------------------------------------------------------
# rolling_stats
# ---------------------------------------------------------------------------

class TestRollingStats:
    def test_empty_journal_zeros(self):
        stats = rolling_stats([])
        for axis in NUMERIC_FIELDS:
            assert stats[axis]["std"] == 0.0
            assert stats[axis]["mean"] == 0.0

    def test_single_entry_std_zero(self):
        journal = _make_journal([(0.5, 0.6, 0.4, 0.9, 0.7, 0.3)])
        stats = rolling_stats(journal)
        assert stats["valence"]["std"] == 0.0
        assert stats["valence"]["mean"] == 0.5

    def test_flat_journal_std_zero(self):
        journal = _make_journal([(0.5, 0.6, 0.4, 0.9, 0.7, 0.3)] * 5)
        stats = rolling_stats(journal)
        for axis in NUMERIC_FIELDS:
            assert stats[axis]["std"] == 0.0

    def test_varied_journal_std_positive(self):
        journal = _make_journal([
            (0.1, 0.2, 0.3, 0.4, 0.5, 0.6),
            (0.9, 0.8, 0.7, 0.6, 0.5, 0.4),
            (0.5, 0.5, 0.5, 0.5, 0.5, 0.5),
        ])
        stats = rolling_stats(journal)
        assert stats["valence"]["std"] > 0
        assert stats["valence"]["min"] == 0.1
        assert stats["valence"]["max"] == 0.9

    def test_uses_only_last_n(self):
        old = [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)] * 30
        new = [(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)] * 5
        journal = _make_journal(old + new)
        stats = rolling_stats(journal, n=5)
        # last 5 are all 1.0 → mean 1.0, std 0
        assert stats["valence"]["mean"] == 1.0
        assert stats["valence"]["std"] == 0.0


# ---------------------------------------------------------------------------
# circadian_baseline
# ---------------------------------------------------------------------------

class TestCircadianBaseline:
    def test_arousal_trough_at_4am(self):
        cb = circadian_baseline(datetime(2026, 5, 16, 4, 0, tzinfo=JST))
        assert cb["arousal"] < 0.30

    def test_arousal_peak_at_2pm(self):
        cb = circadian_baseline(datetime(2026, 5, 16, 14, 0, tzinfo=JST))
        assert cb["arousal"] > 0.75

    def test_arousal_smooth_adjacent_hours(self):
        prev = circadian_baseline(datetime(2026, 5, 16, 13, 0, tzinfo=JST))
        cur = circadian_baseline(datetime(2026, 5, 16, 14, 0, tzinfo=JST))
        nxt = circadian_baseline(datetime(2026, 5, 16, 15, 0, tzinfo=JST))
        assert abs(cur["arousal"] - prev["arousal"]) < 0.1
        assert abs(nxt["arousal"] - cur["arousal"]) < 0.1

    def test_arousal_minute_granularity(self):
        h14 = circadian_baseline(datetime(2026, 5, 16, 14, 0, tzinfo=JST))
        h1430 = circadian_baseline(datetime(2026, 5, 16, 14, 30, tzinfo=JST))
        assert h14["arousal"] != h1430["arousal"]
        assert abs(h14["arousal"] - h1430["arousal"]) < 0.05

    def test_anxiety_night_bump(self):
        night = circadian_baseline(datetime(2026, 5, 16, 3, 0, tzinfo=JST))
        day = circadian_baseline(datetime(2026, 5, 16, 10, 0, tzinfo=JST))
        assert night["anxiety"] > day["anxiety"]

    def test_anxiety_peak_at_330(self):
        peak = circadian_baseline(datetime(2026, 5, 16, 3, 30, tzinfo=JST))
        before = circadian_baseline(datetime(2026, 5, 16, 2, 30, tzinfo=JST))
        after = circadian_baseline(datetime(2026, 5, 16, 4, 30, tzinfo=JST))
        assert peak["anxiety"] >= before["anxiety"]
        assert peak["anxiety"] >= after["anxiety"]


# ---------------------------------------------------------------------------
# daily_mood_seed
# ---------------------------------------------------------------------------

class TestDailyMoodSeed:
    def test_same_day_same_label(self):
        m1 = daily_mood_seed(datetime(2026, 5, 16, 8, 0, tzinfo=JST))
        m2 = daily_mood_seed(datetime(2026, 5, 16, 20, 0, tzinfo=JST))
        assert m1.label == m2.label
        assert m1.date_key == m2.date_key

    def test_same_day_same_axis_bias(self):
        m1 = daily_mood_seed(datetime(2026, 5, 16, 0, 0, tzinfo=JST))
        m2 = daily_mood_seed(datetime(2026, 5, 16, 23, 59, tzinfo=JST))
        assert m1.axis_bias == m2.axis_bias

    def test_different_days_different_labels_sometimes(self):
        labels = {
            daily_mood_seed(datetime(2026, 5, d, 12, 0, tzinfo=JST)).label
            for d in range(1, 32)
        }
        # 31 days → expect at least a few distinct labels from 12-pool
        assert len(labels) >= 3

    def test_axis_bias_within_amplitude(self):
        for d in range(1, 32):
            mood = daily_mood_seed(datetime(2026, 5, d, 12, 0, tzinfo=JST))
            for axis, bias in mood.axis_bias.items():
                assert abs(bias) <= MOOD_BIAS_AMPLITUDE + 1e-9

    def test_label_in_pool(self):
        for d in range(1, 32):
            mood = daily_mood_seed(datetime(2026, 5, d, 12, 0, tzinfo=JST))
            assert mood.label in MOOD_LABELS_POOL


# ---------------------------------------------------------------------------
# tick_jitter
# ---------------------------------------------------------------------------

class TestTickJitter:
    def test_same_tick_reproducible(self):
        j1 = tick_jitter(datetime(2026, 5, 16, 12, 0, tzinfo=JST))
        j2 = tick_jitter(datetime(2026, 5, 16, 12, 0, tzinfo=JST))
        assert j1 == j2

    def test_different_ticks_different_values(self):
        j1 = tick_jitter(datetime(2026, 5, 16, 12, 0, tzinfo=JST))
        j2 = tick_jitter(datetime(2026, 5, 16, 12, 30, tzinfo=JST))
        assert j1 != j2

    def test_within_amplitude(self):
        for h in range(0, 24):
            j = tick_jitter(datetime(2026, 5, 16, h, 0, tzinfo=JST))
            for axis, val in j.items():
                assert abs(val) <= TICK_JITTER_AMPLITUDE + 1e-9


# ---------------------------------------------------------------------------
# max_axis_jump
# ---------------------------------------------------------------------------

class TestMaxAxisJump:
    def test_zero_jump(self):
        prev = _flat_state()
        new = dict(prev)
        assert max_axis_jump(prev, new) == 0.0

    def test_picks_max_across_axes(self):
        prev = _flat_state()
        new = dict(prev)
        new["valence"] = 0.6  # +0.10
        new["affection"] = 0.8  # +0.30
        new["curiosity"] = 0.5  # 0
        assert max_axis_jump(prev, new) == pytest.approx(0.30)

    def test_accepts_dataclass_prev(self):
        from services.emotion.state import EmotionState
        prev = EmotionState()  # uses defaults
        new = {axis: getattr(prev, axis) for axis in NUMERIC_FIELDS}
        new["valence"] = prev.valence + 0.25
        assert max_axis_jump(prev, new) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# perturb
# ---------------------------------------------------------------------------

class TestPerturb:
    def test_output_within_bounds(self):
        # Even with extreme inputs, BOUNDS hold
        rng = random.Random(0)
        for _ in range(100):
            numerics = {axis: random.uniform(*BOUNDS[axis]) for axis in NUMERIC_FIELDS}
            out, _ = perturb(
                numerics, prev_state=numerics, journal=[],
                now=datetime(2026, 5, 16, 12, 0, tzinfo=JST),
                mode="silence", rng=rng,
            )
            for axis in NUMERIC_FIELDS:
                lo, hi = BOUNDS[axis]
                assert lo <= out[axis] <= hi

    def test_flat_journal_amplifies_noise(self):
        """When rolling std < FLAT_STD_THRESHOLD, σ is multiplied."""
        # Compare variance of perturb outputs given flat vs varied journal
        flat_journal = _make_journal([(0.5, 0.5, 0.5, 0.5, 0.5, 0.5)] * 25)
        varied_journal = _make_journal([
            (i / 25, i / 25, i / 25, i / 25, i / 25, i / 25)
            for i in range(25)
        ])
        flat_outputs = []
        varied_outputs = []
        now = datetime(2026, 5, 16, 12, 0, tzinfo=JST)
        for seed in range(100):
            rng = random.Random(seed)
            o1, _ = perturb(
                _flat_state(), prev_state=_flat_state(),
                journal=flat_journal, now=now, mode="silence", rng=rng,
            )
            flat_outputs.append(o1["valence"])
            rng = random.Random(seed)
            o2, _ = perturb(
                _flat_state(), prev_state=_flat_state(),
                journal=varied_journal, now=now, mode="silence", rng=rng,
            )
            varied_outputs.append(o2["valence"])
        import statistics
        flat_std = statistics.pstdev(flat_outputs)
        varied_std = statistics.pstdev(varied_outputs)
        # flat journal should produce wider output spread (after FLAT_NOISE_MULTIPLIER)
        assert flat_std > varied_std

    def test_silence_sigma_larger_than_conversation(self):
        """silence mode produces more variance than conversation mode."""
        now = datetime(2026, 5, 16, 12, 0, tzinfo=JST)
        # use varied journal so flat-multiplier doesn't kick in
        varied_journal = _make_journal([
            (i / 25, i / 25, i / 25, i / 25, i / 25, i / 25) for i in range(25)
        ])
        silence_outputs = []
        conv_outputs = []
        for seed in range(200):
            rng = random.Random(seed)
            o, _ = perturb(
                _flat_state(), prev_state=_flat_state(),
                journal=varied_journal, now=now, mode="silence", rng=rng,
            )
            silence_outputs.append(o["valence"])
            rng = random.Random(seed)
            o, _ = perturb(
                _flat_state(), prev_state=_flat_state(),
                journal=varied_journal, now=now, mode="conversation", rng=rng,
            )
            conv_outputs.append(o["valence"])
        import statistics
        # silence σ > conversation σ for valence (0.05 vs 0.03)
        assert statistics.pstdev(silence_outputs) > statistics.pstdev(conv_outputs)

    def test_night_silence_arousal_pulled_down(self):
        """4am + silence with arousal=0.85 → output < 0.85 on average."""
        now = datetime(2026, 5, 16, 4, 0, tzinfo=JST)
        initial = {axis: 0.85 if axis == "arousal" else 0.5 for axis in NUMERIC_FIELDS}
        outputs = []
        for seed in range(200):
            rng = random.Random(seed)
            o, _ = perturb(
                initial, prev_state=initial, journal=[],
                now=now, mode="silence", rng=rng,
            )
            outputs.append(o["arousal"])
        import statistics
        assert statistics.mean(outputs) < 0.85
        # Significant drop expected at night pull 0.15 + baseline 0.55 mean-rev 0.02
        assert statistics.mean(outputs) < 0.80

    def test_conversation_no_night_arousal_pull(self):
        """4am + conversation: weaker pull than silence (no night boost)."""
        now = datetime(2026, 5, 16, 4, 0, tzinfo=JST)
        initial = {axis: 0.85 if axis == "arousal" else 0.5 for axis in NUMERIC_FIELDS}
        silence_outs = []
        conv_outs = []
        for seed in range(200):
            rng = random.Random(seed)
            silence_outs.append(perturb(
                initial, prev_state=initial, journal=[],
                now=now, mode="silence", rng=rng,
            )[0]["arousal"])
            rng = random.Random(seed)
            conv_outs.append(perturb(
                initial, prev_state=initial, journal=[],
                now=now, mode="conversation", rng=rng,
            )[0]["arousal"])
        import statistics
        # silence pulls arousal down more (night boost + mean-reversion)
        assert statistics.mean(silence_outs) < statistics.mean(conv_outs)

    def test_mean_reversion_silence_only(self):
        """Off-baseline value reverts toward baseline in silence, not in conversation.

        Use affection (strong reversion 0.06) far from baseline (0.82): start at 0.30.
        """
        now = datetime(2026, 5, 16, 12, 0, tzinfo=JST)  # noon, no circadian
        initial = {axis: _TEST_BASELINE[axis] for axis in NUMERIC_FIELDS}
        initial["affection"] = 0.30  # far below baseline 0.82
        silence_deltas = []
        conv_deltas = []
        for seed in range(200):
            rng = random.Random(seed)
            o, _ = perturb(
                initial, prev_state=initial, journal=[],
                now=now, mode="silence", rng=rng,
            )
            silence_deltas.append(o["affection"] - 0.30)
            rng = random.Random(seed)
            o, _ = perturb(
                initial, prev_state=initial, journal=[],
                now=now, mode="conversation", rng=rng,
            )
            conv_deltas.append(o["affection"] - 0.30)
        import statistics
        # silence should pull upward toward 0.82 on average
        assert statistics.mean(silence_deltas) > 0.01
        # conversation has no mean-reversion → mean ≈ 0 (only noise + jitter)
        assert abs(statistics.mean(conv_deltas)) < statistics.mean(silence_deltas)

    def test_mean_reversion_skipped_when_baseline_missing(self, monkeypatch):
        """When load_baseline() returns None, silence-mode reversion is skipped.

        Pin a far-from-anchor affection value; without a baseline the average
        delta should be ~0 (just noise + jitter), not the upward pull we see
        when a baseline is present.
        """
        monkeypatch.setattr(
            "services.emotion.dynamics.load_baseline",
            lambda *a, **kw: None,
        )
        now = datetime(2026, 5, 16, 12, 0, tzinfo=JST)
        initial = {axis: 0.5 for axis in NUMERIC_FIELDS}
        initial["affection"] = 0.30
        deltas = []
        for seed in range(200):
            rng = random.Random(seed)
            o, _ = perturb(
                initial, prev_state=initial, journal=[],
                now=now, mode="silence", rng=rng,
            )
            deltas.append(o["affection"] - 0.30)
        import statistics
        # No baseline → no directional pull. Mean is dominated by zero-mean
        # noise + tiny jitter; absolute mean should be small.
        assert abs(statistics.mean(deltas)) < 0.01

    def test_perturbed_axes_marks_large_moves(self):
        """Force a large move on one axis via flat journal + force seed."""
        now = datetime(2026, 5, 16, 4, 0, tzinfo=JST)  # night = strong arousal pull
        initial = {axis: 0.5 for axis in NUMERIC_FIELDS}
        initial["arousal"] = 0.95  # strong pull-down expected
        rng = random.Random(42)
        _, perturbed = perturb(
            initial, prev_state=initial, journal=[],
            now=now, mode="silence", rng=rng,
        )
        # arousal 0.95 → 0.24 baseline at 4am, pull 0.15 × (0.24 - 0.95) ≈ -0.107
        # plus mean-reversion 0.02 × (0.55 - 0.95) = -0.008 plus noise ≈ -0.11
        # → moves > LABEL_RESET_THRESHOLD (0.05)
        assert "arousal" in perturbed

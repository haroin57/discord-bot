"""Tests for services.emotion.validator — validate_and_apply + _clamp01."""

import random

import pytest

from services.emotion.state import EmotionState, LastInteraction
from services.emotion.validator import _clamp01, validate_and_apply

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_OUTPUT = {
    "valence": 0.5,
    "arousal": 0.4,
    "loneliness": 0.3,
    "affection": 0.8,
    "curiosity": 0.6,
    "anxiety": 0.2,
}


def _base_state(**kwargs) -> EmotionState:
    return EmotionState(**kwargs)


@pytest.fixture(autouse=True)
def disable_dynamics_v2(monkeypatch):
    """v3 tests assume LLM-only behavior. v2 perturb is tested separately
    below (TestValidateAndApplyWithDynamicsV2)."""
    monkeypatch.setattr(
        "services.emotion.params.EMOTION_DYNAMICS_V2", False,
    )


# ---------------------------------------------------------------------------
# validate_and_apply
# ---------------------------------------------------------------------------

class TestValidateAndApply:
    def test_validate_valid_output(self):
        state = _base_state()
        result = validate_and_apply(state, _VALID_OUTPUT, mode="silence")
        assert result is not None
        assert result.valence == pytest.approx(0.5)
        assert result.arousal == pytest.approx(0.4)
        assert result.loneliness == pytest.approx(0.3)
        assert result.affection == pytest.approx(0.8)
        assert result.curiosity == pytest.approx(0.6)
        assert result.anxiety == pytest.approx(0.2)

    def test_validate_missing_field_returns_none(self):
        output = dict(_VALID_OUTPUT)
        del output["arousal"]
        result = validate_and_apply(_base_state(), output, mode="silence")
        assert result is None

    def test_validate_non_numeric_returns_none(self):
        output = dict(_VALID_OUTPUT)
        output["valence"] = "abc"
        result = validate_and_apply(_base_state(), output, mode="silence")
        assert result is None

    def test_validate_clamps_to_bounds(self):
        output = dict(_VALID_OUTPUT)
        output["valence"] = 5.0   # should clamp to 1.0
        output["anxiety"] = -0.5  # should clamp to 0.0
        result = validate_and_apply(_base_state(), output, mode="silence")
        assert result is not None
        assert result.valence == pytest.approx(1.0)
        assert result.anxiety == pytest.approx(0.0)

    def test_validate_derived_labels(self):
        output = dict(_VALID_OUTPUT)
        output["derived_labels"] = ["shy", "curious"]
        result = validate_and_apply(_base_state(), output, mode="silence")
        assert result is not None
        assert result.derived_labels == ["shy", "curious"]

    def test_validate_labels_dict(self):
        output = dict(_VALID_OUTPUT)
        output["labels"] = {"primary": "嬉しい"}
        result = validate_and_apply(_base_state(), output, mode="silence")
        assert result is not None
        assert result.labels == {"primary": "嬉しい"}

    def test_validate_journal_appended(self):
        state = _base_state()
        assert len(state.emotion_journal) == 0
        result = validate_and_apply(state, _VALID_OUTPUT, mode="silence")
        assert result is not None
        assert len(result.emotion_journal) == 1
        entry = result.emotion_journal[0]
        assert entry["v"] == pytest.approx(0.5)
        assert entry["source"] == "silence"

    def test_validate_journal_max_25(self):
        existing = [
            {"ts": f"2026-01-01T00:{i:02d}:00+09:00", "v": 0.5, "ar": 0.4,
             "lo": 0.3, "af": 0.8, "cu": 0.6, "ax": 0.2,
             "trigger": "", "source": "silence"}
            for i in range(25)
        ]
        state = EmotionState(emotion_journal=existing)
        assert len(state.emotion_journal) == 25
        result = validate_and_apply(state, _VALID_OUTPUT, mode="silence")
        assert result is not None
        assert len(result.emotion_journal) == 25

    def test_validate_conversation_mode_stamps_interaction(self):
        meta = {
            "topics": ["音楽", "仕事"],
            "quality": 0.9,
            "senpai_engagement": 0.85,
        }
        result = validate_and_apply(
            _base_state(), _VALID_OUTPUT,
            mode="conversation", conversation_meta=meta,
        )
        assert result is not None
        li = result.last_interaction
        assert li.timestamp is not None
        assert li.quality == pytest.approx(0.9)
        assert li.senpai_responsiveness == pytest.approx(0.85)
        assert "音楽" in li.topics
        assert "仕事" in li.topics

    def test_validate_silence_mode_no_interaction(self):
        original_li = LastInteraction(timestamp="2026-01-01T00:00:00+09:00")
        state = EmotionState(last_interaction=original_li)
        result = validate_and_apply(state, _VALID_OUTPUT, mode="silence")
        assert result is not None
        # silence mode must not touch last_interaction
        assert result.last_interaction.timestamp == "2026-01-01T00:00:00+09:00"

    def test_silence_hours_kwarg_ignored(self):
        """Callers using the old signature with silence_hours= must not blow up."""
        result = validate_and_apply(
            _base_state(), _VALID_OUTPUT,
            mode="silence", silence_hours=6.0,
        )
        assert result is not None


# ---------------------------------------------------------------------------
# _clamp01
# ---------------------------------------------------------------------------

class TestClamp01:
    def test_clamp01_normal(self):
        assert _clamp01(0.5) == pytest.approx(0.5)

    def test_clamp01_over(self):
        assert _clamp01(1.5) == pytest.approx(1.0)

    def test_clamp01_under(self):
        assert _clamp01(-0.1) == pytest.approx(0.0)

    def test_clamp01_invalid(self):
        assert _clamp01("abc") == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# v4 dynamics integration — perturb hook + label reset
# ---------------------------------------------------------------------------

class TestValidateAndApplyWithDynamicsV2:
    """Verify v4 perturb hook fires correctly when EMOTION_DYNAMICS_V2=True."""

    @pytest.fixture(autouse=True)
    def enable_dynamics_v2(self, monkeypatch):
        """Override the module-level disable fixture for this class."""
        monkeypatch.setattr(
            "services.emotion.params.EMOTION_DYNAMICS_V2", True,
        )
        # Seed the global RNG used by perturb so behavior is deterministic.
        random.seed(0)

    def test_perturb_fires_when_llm_stays_still(self):
        """LLM returning identical state → perturb adds variance."""
        prev = EmotionState(
            valence=0.5, arousal=0.5, loneliness=0.5,
            affection=0.5, curiosity=0.5, anxiety=0.5,
        )
        # LLM output that matches prev exactly (zero jump)
        identical = {
            "valence": 0.5, "arousal": 0.5, "loneliness": 0.5,
            "affection": 0.5, "curiosity": 0.5, "anxiety": 0.5,
        }
        result = validate_and_apply(prev, identical, mode="silence")
        assert result is not None
        # At least one axis should have moved from 0.5
        moved = [
            axis for axis in ("valence", "arousal", "loneliness",
                              "affection", "curiosity", "anxiety")
            if abs(getattr(result, axis) - 0.5) > 0.001
        ]
        assert moved, "perturb should have moved at least one axis"

    def test_perturb_skipped_on_large_llm_jump(self):
        """If LLM moves an axis > LARGE_LLM_JUMP_THRESHOLD, perturb is skipped."""
        prev = EmotionState(
            valence=0.0, arousal=0.5, loneliness=0.5,
            affection=0.5, curiosity=0.5, anxiety=0.5,
        )
        # LLM moves valence from 0.0 to 0.9 (jump = 0.9, > threshold 0.12)
        big = {
            "valence": 0.9, "arousal": 0.5, "loneliness": 0.5,
            "affection": 0.5, "curiosity": 0.5, "anxiety": 0.5,
        }
        result = validate_and_apply(prev, big, mode="silence")
        assert result is not None
        # When skipped, output equals LLM output (clamped) exactly
        assert result.valence == pytest.approx(0.9)
        assert result.arousal == pytest.approx(0.5)

    def test_perturbed_axes_have_labels_cleared(self):
        """Axes moved >= LABEL_RESET_THRESHOLD should drop their labels."""
        # Force a large perturb on arousal by starting at night with arousal=0.95
        # so circadian pull moves it significantly.
        prev = EmotionState(
            valence=0.5, arousal=0.95, loneliness=0.5,
            affection=0.5, curiosity=0.5, anxiety=0.5,
            labels={
                "valence": "機嫌よし",
                "arousal": "高ぶっている",
                "loneliness": "普通",
                "affection": "強い好意",
                "curiosity": "普通",
                "anxiety": "やや不安",
            },
        )
        # LLM stays at 0.95 arousal — perturb at 4am should pull it down hard
        output = {
            "valence": 0.5, "arousal": 0.95, "loneliness": 0.5,
            "affection": 0.5, "curiosity": 0.5, "anxiety": 0.5,
            "labels": {
                "valence": "機嫌よし",
                "arousal": "高ぶっている",
                "loneliness": "普通",
                "affection": "強い好意",
                "curiosity": "普通",
                "anxiety": "やや不安",
            },
        }
        # Run multiple times to ensure at least one trial moves arousal
        any_arousal_cleared = False
        for seed in range(20):
            random.seed(seed)
            from unittest.mock import patch
            import services.emotion.validator as v_mod
            from datetime import datetime, timedelta, timezone
            JST = timezone(timedelta(hours=9))
            with patch.object(
                v_mod, "datetime",
            ) as mock_dt:
                mock_dt.now.return_value = datetime(2026, 5, 16, 4, 0, tzinfo=JST)
                mock_dt.fromisoformat = datetime.fromisoformat
                result = validate_and_apply(prev, output, mode="silence")
            assert result is not None
            if "arousal" not in result.labels:
                any_arousal_cleared = True
                # With strengthened noise σ (2026-05-17), other axes may
                # also exceed the LABEL_RESET threshold in the same tick.
                # That's expected; we only require that arousal gets
                # cleared by the strong night-time pull.
                break
        assert any_arousal_cleared, (
            "expected arousal label cleared after large night-time perturb"
        )

    def test_bounds_preserved_after_perturb(self):
        """Even with extreme inputs, perturb output stays in BOUNDS."""
        prev = EmotionState(
            valence=0.95, arousal=0.95, loneliness=0.95,
            affection=0.95, curiosity=0.95, anxiety=0.95,
        )
        output = dict(_VALID_OUTPUT)
        # Stay near previous so perturb fires
        output.update({
            "valence": 0.95, "arousal": 0.95, "loneliness": 0.95,
            "affection": 0.95, "curiosity": 0.95, "anxiety": 0.95,
        })
        for seed in range(50):
            random.seed(seed)
            result = validate_and_apply(prev, output, mode="silence")
            assert result is not None
            assert -1.0 <= result.valence <= 1.0
            for axis in ("arousal", "loneliness", "affection",
                         "curiosity", "anxiety"):
                assert 0.0 <= getattr(result, axis) <= 1.0

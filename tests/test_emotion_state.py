"""Tests for services.emotion.state — atomic JSON load/save + schema fallback."""

import json
import os

import pytest

from services.emotion.params import INITIAL_STATE
from services.emotion.state import (
    EmotionState,
    LastInteraction,
    load_state,
    save_state,
)


@pytest.fixture()
def state_path(tmp_path):
    return tmp_path / "kanae_emotion.json"


class TestLoadSaveRoundtrip:
    def test_default_state_matches_initial_params(self):
        s = EmotionState()
        for k, v in INITIAL_STATE.items():
            assert getattr(s, k) == pytest.approx(v)

    def test_save_then_load_preserves_all_fields(self, state_path):
        original = EmotionState(
            updated_at="2026-05-13T20:00:00+09:00",
            valence=0.55, arousal=0.66,
            loneliness=0.71, affection=0.88, curiosity=0.33,
            anxiety=0.22,
            last_interaction=LastInteraction(
                timestamp="2026-05-13T19:30:00+09:00",
                quality=0.8, initiated_by="senpai",
                topics=["仕事", "映画"], duration_minutes=25.0,
                senpai_responsiveness=0.9,
            ),
            consecutive_self_initiated=3,
        )
        save_state(original, state_path)
        loaded = load_state(state_path)
        assert loaded == original

    def test_legacy_momentum_field_is_ignored(self, state_path):
        """Old state files used to carry `momentum`. The field was removed
        in 2026-05-13 cleanup; loading such files must succeed and silently
        drop the stale key."""
        import json
        state_path.write_text(json.dumps({
            "valence": 0.5,
            "momentum": {"direction": "improving", "duration_hours": 12.5},
        }))
        s = load_state(state_path)
        assert s.valence == pytest.approx(0.5)
        # state has no momentum attribute anymore — verify the structural API
        assert not hasattr(s, "momentum")

    def test_legacy_dominance_satisfaction_fields_ignored(self, state_path):
        """`dominance` and `satisfaction` were removed in 2026-05-13 cleanup
        (dead field + valence overlap). State files that still carry them
        must load successfully with the stale values dropped."""
        import json
        state_path.write_text(json.dumps({
            "valence": 0.5,
            "dominance": 0.7,    # legacy
            "satisfaction": 0.9, # legacy
            "loneliness": 0.4,
        }))
        s = load_state(state_path)
        assert s.valence == pytest.approx(0.5)
        assert s.loneliness == pytest.approx(0.4)
        assert not hasattr(s, "dominance")
        assert not hasattr(s, "satisfaction")

    def test_save_uses_mode_0o600(self, state_path):
        save_state(EmotionState(), state_path)
        mode = os.stat(state_path).st_mode & 0o777
        assert mode == 0o600

    def test_save_atomic_cleans_up_tmp_files(self, state_path):
        save_state(EmotionState(), state_path)
        # tmp files use {name}.{uuid}.tmp; none should survive a clean save
        leftovers = list(state_path.parent.glob(f"{state_path.name}.*.tmp"))
        assert leftovers == []


class TestLoadMissingOrCorrupt:
    def test_missing_file_returns_fresh_default(self, state_path):
        assert not state_path.exists()
        s = load_state(state_path)
        assert isinstance(s, EmotionState)
        assert s.loneliness == pytest.approx(INITIAL_STATE["loneliness"])

    def test_corrupt_json_returns_default(self, state_path):
        state_path.write_text("{not valid json")
        s = load_state(state_path)
        assert isinstance(s, EmotionState)
        assert s.loneliness == pytest.approx(INITIAL_STATE["loneliness"])

    def test_partial_schema_backfills_defaults(self, state_path):
        """Old state files written before a field existed must still load."""
        state_path.write_text(json.dumps({
            "valence": 0.9,
            # everything else missing
        }))
        s = load_state(state_path)
        assert s.valence == pytest.approx(0.9)
        # missing fields fall back to INITIAL_STATE
        assert s.loneliness == pytest.approx(INITIAL_STATE["loneliness"])
        assert s.affection == pytest.approx(INITIAL_STATE["affection"])
        # nested defaults are also restored
        assert isinstance(s.last_interaction, LastInteraction)

    def test_malformed_numeric_falls_back(self, state_path):
        state_path.write_text(json.dumps({"valence": "not-a-number"}))
        s = load_state(state_path)
        assert s.valence == pytest.approx(INITIAL_STATE["valence"])

    def test_topics_must_be_list(self, state_path):
        state_path.write_text(json.dumps({
            "last_interaction": {"topics": "not-a-list"},
        }))
        s = load_state(state_path)
        assert s.last_interaction.topics == []

    def test_topics_capped(self, state_path):
        state_path.write_text(json.dumps({
            "last_interaction": {"topics": [f"t{i}" for i in range(20)]},
        }))
        s = load_state(state_path)
        # state.from_dict caps at 8 to keep prompts compact
        assert len(s.last_interaction.topics) == 8

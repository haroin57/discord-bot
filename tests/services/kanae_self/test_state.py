"""Tests for services.kanae_self.state — load/save + schema tolerance."""

import json

import pytest

from services.kanae_self.state import (
    CURRENT_VERSION,
    MAX_EPISODES,
    Episode,
    Interest,
    KanaeSelfState,
    load_state,
    save_state,
)


@pytest.fixture()
def state_path(tmp_path):
    return tmp_path / "kanae_self.json"


class TestLoadSaveRoundtrip:
    def test_default_state_is_empty(self):
        s = KanaeSelfState()
        assert s.version == CURRENT_VERSION
        assert s.interests == {}
        assert s.episodes == []

    def test_save_then_load_preserves_interests(self, state_path):
        original = KanaeSelfState(
            updated_at="2026-05-15T20:00:00+09:00",
            interests={
                "uk garage": Interest(
                    topic="uk garage",
                    salience=0.78,
                    last_touched_at="2026-05-15T19:30:00+09:00",
                    first_seen_at="2026-05-10T12:00:00+09:00",
                    mention_count=12,
                    valence_attached=0.6,
                    arousal_attached=0.4,
                ),
            },
        )
        save_state(original, state_path)
        loaded = load_state(state_path)
        assert "uk garage" in loaded.interests
        assert loaded.interests["uk garage"].salience == pytest.approx(0.78)
        assert loaded.interests["uk garage"].mention_count == 12

    def test_save_then_load_preserves_episodes(self, state_path):
        original = KanaeSelfState(
            episodes=[
                Episode(
                    id="abc12345",
                    ts="2026-05-15T19:30:00+09:00",
                    channel_id="999",
                    summary="先輩から TAKU INOUE を勧められた",
                    topics=["taku inoue"],
                    valence_delta=0.2,
                    importance=3,
                ),
            ],
        )
        save_state(original, state_path)
        loaded = load_state(state_path)
        assert len(loaded.episodes) == 1
        assert loaded.episodes[0].id == "abc12345"
        assert loaded.episodes[0].importance == 3


class TestSchemaTolerance:
    def test_missing_file_returns_default(self, tmp_path):
        s = load_state(tmp_path / "does-not-exist.json")
        assert s == KanaeSelfState()

    def test_corrupt_json_returns_default(self, state_path):
        state_path.write_text("not valid json {{{")
        s = load_state(state_path)
        assert s == KanaeSelfState()

    def test_unknown_keys_are_dropped(self, state_path):
        state_path.write_text(json.dumps({
            "version": 1,
            "interests": {},
            "episodes": [],
            "future_field_added_in_phase_4": "ignore me",
        }))
        s = load_state(state_path)
        assert s.interests == {}
        assert not hasattr(s, "future_field_added_in_phase_4")

    def test_interest_with_invalid_floats_falls_back_to_defaults(self):
        i = Interest.from_dict({
            "topic": "rust",
            "salience": "not a number",
            "valence_attached": None,
        })
        assert i.topic == "rust"
        assert 0.0 <= i.salience <= 1.0
        assert i.valence_attached == 0.0

    def test_interest_clamps_out_of_range_values(self):
        i = Interest.from_dict({
            "topic": "x",
            "salience": 99.0,
            "valence_attached": -5.0,
            "arousal_attached": 7.0,
        })
        assert i.salience == 1.0
        assert i.valence_attached == -1.0
        assert i.arousal_attached == 1.0

    def test_episodes_truncated_to_max(self, state_path):
        eps = [
            {
                "id": f"id-{i:04d}",
                "ts": f"2026-05-15T{i:02d}:00:00+09:00",
                "summary": f"episode {i}",
            }
            for i in range(MAX_EPISODES + 10)
        ]
        state_path.write_text(json.dumps({"episodes": eps}))
        s = load_state(state_path)
        assert len(s.episodes) == MAX_EPISODES
        # The newest (highest index) episodes survive.
        assert s.episodes[-1].id == f"id-{MAX_EPISODES + 9:04d}"

    def test_topic_keys_normalized_to_lowercase_on_load(self, state_path):
        # If a writer stored with mixed case, load should canonicalize via
        # the matching-topic check (drops mismatched dict-key vs Interest.topic).
        state_path.write_text(json.dumps({
            "interests": {
                "uk garage": {"topic": "uk garage", "salience": 0.5},
                "MISMATCHED": {"topic": "different", "salience": 0.5},
            },
        }))
        s = load_state(state_path)
        assert "uk garage" in s.interests
        # MISMATCHED must NOT survive — the dict key didn't match the
        # Interest.topic, so the schema-load drops it as a safety check.
        assert "MISMATCHED" not in s.interests
        assert "different" not in s.interests


class TestFilePermissions:
    def test_save_writes_with_0o600(self, state_path):
        save_state(KanaeSelfState(), state_path)
        import stat
        mode = state_path.stat().st_mode & 0o777
        assert mode == 0o600


class TestInterestChannelId:
    def test_loads_legacy_dict_without_channel_id(self):
        it = Interest.from_dict({"topic": "x", "salience": 0.5})
        assert it.channel_id == ""

    def test_roundtrips_channel_id(self, state_path):
        original = KanaeSelfState(
            interests={
                "uk garage": Interest(
                    topic="uk garage",
                    salience=0.7,
                    channel_id="1489297911895294042",
                ),
            },
        )
        save_state(original, state_path)
        reloaded = load_state(state_path)
        assert reloaded.interests["uk garage"].channel_id == "1489297911895294042"

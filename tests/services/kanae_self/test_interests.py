"""Tests for services.kanae_self.interests — decay / upsert / top-k."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from services.kanae_self.interests import (
    DECAY_HALF_LIFE_HOURS,
    GC_THRESHOLD,
    TOUCH_BUMP,
    decay_and_upsert_interests,
    top_interests,
)
from services.kanae_self.state import MAX_INTERESTS, Interest, KanaeSelfState

_JST = timezone(timedelta(hours=9))


def _at(year=2026, month=5, day=15, hour=20, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=_JST)


class TestUpsertNew:
    def test_new_topic_gets_baseline_salience_plus_bump(self):
        state = KanaeSelfState()
        now = _at()
        decay_and_upsert_interests(
            state,
            [{"topic": "uk garage", "valence": 0.5, "arousal": 0.4}],
            now=now,
        )
        assert "uk garage" in state.interests
        i = state.interests["uk garage"]
        # 0.3 baseline + 0.15 bump
        assert i.salience == pytest.approx(0.3 + TOUCH_BUMP)
        assert i.mention_count == 1
        assert i.valence_attached == pytest.approx(0.5)
        assert i.arousal_attached == pytest.approx(0.4)
        assert i.last_touched_at == now.isoformat(timespec="seconds")

    def test_topic_normalization_lowercases_and_trims(self):
        state = KanaeSelfState()
        decay_and_upsert_interests(
            state, [{"topic": "  UK Garage  "}], now=_at(),
        )
        assert "uk garage" in state.interests

    def test_empty_topic_is_dropped(self):
        state = KanaeSelfState()
        decay_and_upsert_interests(
            state, [{"topic": ""}, {"topic": "   "}, {}], now=_at(),
        )
        assert state.interests == {}


class TestUpsertExisting:
    def test_existing_topic_gets_bump_and_mention_count_increment(self):
        state = KanaeSelfState(interests={
            "rust async": Interest(
                topic="rust async",
                salience=0.4,
                last_touched_at=_at(hour=19).isoformat(timespec="seconds"),
                first_seen_at=_at(hour=10).isoformat(timespec="seconds"),
                mention_count=3,
                valence_attached=0.0,
                arousal_attached=0.0,
            ),
        })
        # 1 hour gap — light decay then bump.
        now = _at(hour=20)
        decay_and_upsert_interests(
            state,
            [{"topic": "rust async", "valence": 0.6, "arousal": 0.5}],
            now=now,
        )
        i = state.interests["rust async"]
        assert i.mention_count == 4
        # Valence drifts toward +0.6 via EMA (alpha=0.3): 0.0 → 0.18
        assert i.valence_attached == pytest.approx(0.18, abs=1e-3)
        # Salience: 0.4 * decay(1h) + 0.15 ≈ 0.40 + 0.15 = ~0.55
        assert i.salience > 0.4

    def test_salience_bumps_clamp_at_one(self):
        state = KanaeSelfState(interests={
            "topic": Interest(
                topic="topic",
                salience=0.95,
                last_touched_at=_at(hour=20).isoformat(timespec="seconds"),
            ),
        })
        decay_and_upsert_interests(
            state, [{"topic": "topic"}], now=_at(hour=20, minute=1),
        )
        assert state.interests["topic"].salience <= 1.0


class TestDecay:
    def test_no_decay_at_zero_elapsed(self):
        state = KanaeSelfState(interests={
            "x": Interest(
                topic="x",
                salience=0.8,
                last_touched_at=_at(hour=20).isoformat(timespec="seconds"),
            ),
        })
        decay_and_upsert_interests(state, [], now=_at(hour=20))
        # Untouched + zero elapsed → salience stays as-is.
        assert state.interests["x"].salience == pytest.approx(0.8)

    def test_half_life_halves_salience(self):
        state = KanaeSelfState(interests={
            "x": Interest(
                topic="x",
                salience=0.8,
                last_touched_at=_at(hour=20).isoformat(timespec="seconds"),
            ),
        })
        later = _at(hour=20) + timedelta(hours=DECAY_HALF_LIFE_HOURS)
        decay_and_upsert_interests(state, [], now=later)
        assert state.interests["x"].salience == pytest.approx(0.4, abs=1e-3)

    def test_decay_below_threshold_gcs_topic(self):
        state = KanaeSelfState(interests={
            "x": Interest(
                topic="x",
                salience=0.1,
                last_touched_at=_at(hour=20).isoformat(timespec="seconds"),
            ),
        })
        # 10 half-lives → salience ≈ 0.0001, below GC_THRESHOLD
        later = _at(hour=20) + timedelta(hours=DECAY_HALF_LIFE_HOURS * 10)
        decay_and_upsert_interests(state, [], now=later)
        assert "x" not in state.interests

    def test_touched_topic_survives_even_if_pre_decay_below_threshold(self):
        state = KanaeSelfState(interests={
            "x": Interest(
                topic="x",
                salience=0.06,
                last_touched_at=_at(hour=20).isoformat(timespec="seconds"),
            ),
        })
        # Long elapsed — pre-bump salience would GC, but a touch this turn
        # bumps it back above threshold.
        later = _at(hour=20) + timedelta(hours=DECAY_HALF_LIFE_HOURS * 2)
        decay_and_upsert_interests(state, [{"topic": "x"}], now=later)
        assert "x" in state.interests
        assert state.interests["x"].salience >= GC_THRESHOLD


class TestCapAndOrdering:
    def test_max_interests_cap_keeps_highest_salience(self):
        state = KanaeSelfState()
        now = _at()
        # Seed MAX_INTERESTS + 5 distinct topics with varying salience.
        for i in range(MAX_INTERESTS + 5):
            sal = (i + 1) / (MAX_INTERESTS + 10)  # 0..1 ascending
            state.interests[f"t{i}"] = Interest(
                topic=f"t{i}",
                salience=sal,
                last_touched_at=now.isoformat(timespec="seconds"),
            )
        # Trigger the cap by upserting a fresh topic.
        decay_and_upsert_interests(state, [{"topic": "fresh"}], now=now)
        assert len(state.interests) <= MAX_INTERESTS
        # The lowest-salience seed (`t0`, `t1`, ...) must be dropped.
        assert "t0" not in state.interests

    def test_top_interests_orders_by_salience(self):
        state = KanaeSelfState(interests={
            "a": Interest(topic="a", salience=0.2),
            "b": Interest(topic="b", salience=0.8),
            "c": Interest(topic="c", salience=0.5),
        })
        top = top_interests(state, k=2)
        assert [i.topic for i in top] == ["b", "c"]

    def test_top_interests_zero_k_returns_empty(self):
        state = KanaeSelfState(interests={"a": Interest(topic="a", salience=0.5)})
        assert top_interests(state, k=0) == []

    def test_top_interests_empty_state_returns_empty(self):
        assert top_interests(KanaeSelfState(), k=5) == []


class TestFormatBlock:
    def test_format_empty_state_returns_empty_string(self):
        from services.kanae_self.format import format_interests_block
        assert format_interests_block(KanaeSelfState()) == ""

    def test_format_includes_topic_and_affect_label(self):
        from services.kanae_self.format import format_interests_block
        now = _at(hour=20)
        state = KanaeSelfState(interests={
            "uk garage": Interest(
                topic="uk garage",
                salience=0.78,
                last_touched_at=(now - timedelta(hours=3)).isoformat(timespec="seconds"),
                mention_count=12,
                valence_attached=0.6,
            ),
        })
        block = format_interests_block(state, now=now)
        assert "<kanae_interests" in block
        assert "uk garage" in block
        assert "好意" in block
        assert "12回" in block
        assert "3時間前" in block
        assert block.endswith("</kanae_interests>")


class TestChannelIdAwareness:
    def test_decay_and_upsert_records_channel_id_on_new_topic(self):
        state = KanaeSelfState()
        decay_and_upsert_interests(
            state, [{"topic": "bird", "valence": 0.0, "arousal": 0.0}],
            channel_id="1489297911895294042",
        )
        assert state.interests["bird"].channel_id == "1489297911895294042"

    def test_decay_and_upsert_overwrites_channel_id_on_existing_topic(self):
        state = KanaeSelfState(
            interests={"bird": Interest(topic="bird", salience=0.5, channel_id="111")},
        )
        decay_and_upsert_interests(
            state, [{"topic": "bird"}], channel_id="222",
        )
        assert state.interests["bird"].channel_id == "222"

    def test_decay_and_upsert_preserves_channel_id_when_caller_omits(self):
        state = KanaeSelfState(
            interests={"bird": Interest(topic="bird", salience=0.5, channel_id="111")},
        )
        decay_and_upsert_interests(state, [{"topic": "bird"}])
        assert state.interests["bird"].channel_id == "111"

    def test_top_interests_no_channel_id_unchanged_ordering(self):
        state = KanaeSelfState(interests={
            "a": Interest(topic="a", salience=0.4, channel_id="111"),
            "b": Interest(topic="b", salience=0.85, channel_id="222"),
        })
        result = top_interests(state, k=2)
        assert [it.topic for it in result] == ["b", "a"]

    def test_top_interests_same_channel_bonus_lifts_over_moderate_global(self):
        state = KanaeSelfState(interests={
            "local": Interest(topic="local", salience=0.4, channel_id="111"),
            "global": Interest(topic="global", salience=0.85, channel_id="222"),
        })
        result = top_interests(state, k=2, channel_id="111")
        assert [it.topic for it in result] == ["local", "global"]

    def test_top_interests_same_channel_bonus_does_not_overcome_hot_global(self):
        state = KanaeSelfState(interests={
            "local": Interest(topic="local", salience=0.4, channel_id="111"),
            "hot": Interest(topic="hot", salience=0.95, channel_id="222"),
        })
        result = top_interests(state, k=2, channel_id="111")
        assert [it.topic for it in result] == ["hot", "local"]

    def test_top_interests_empty_record_channel_no_bonus(self):
        state = KanaeSelfState(interests={
            "legacy": Interest(topic="legacy", salience=0.6, channel_id=""),
            "local": Interest(topic="local", salience=0.5, channel_id="111"),
        })
        result = top_interests(state, k=2, channel_id="111")
        # local (0.5 + 0.5 = 1.0) > legacy (0.6 + 0 = 0.6)
        assert [it.topic for it in result] == ["local", "legacy"]

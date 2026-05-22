"""Tests for services.kanae_self.recall — query augmentation with interest topics."""

import pytest

from services.kanae_self.recall import augment_query_with_interests, top_interest_topics
from services.kanae_self.state import Interest, KanaeSelfState


@pytest.fixture
def state_with_interests():
    state = KanaeSelfState()
    state.interests = {
        "uk garage": Interest(topic="uk garage", salience=0.9),
        "rust": Interest(topic="rust", salience=0.7),
        "ableton": Interest(topic="ableton", salience=0.5),
    }
    return state


class TestAugmentQueryWithInterests:
    def test_augment_with_interests(self, state_with_interests):
        result = augment_query_with_interests("query", state_with_interests, k=3)
        assert result == "query | topics: uk garage / rust / ableton"

    def test_augment_no_state(self):
        result = augment_query_with_interests("query", None)
        assert result == "query"

    def test_augment_empty_query(self, state_with_interests):
        result = augment_query_with_interests("", state_with_interests)
        assert result == ""

    def test_augment_k_zero(self, state_with_interests):
        result = augment_query_with_interests("query", state_with_interests, k=0)
        assert result == "query"

    def test_augment_no_interests(self):
        state = KanaeSelfState()
        result = augment_query_with_interests("query", state)
        assert result == "query"


class TestTopInterestTopics:
    def test_top_interest_topics_basic(self, state_with_interests):
        result = top_interest_topics(state_with_interests, k=3)
        assert result == ["uk garage", "rust", "ableton"]

    def test_top_interest_topics_none_state(self):
        result = top_interest_topics(None)
        assert result == []

    def test_top_interest_topics_k_zero(self, state_with_interests):
        result = top_interest_topics(state_with_interests, k=0)
        assert result == []


class TestRecallChannelAware:
    def test_augment_query_with_channel_id_prioritizes_local_topics(self):
        from services.kanae_self.state import Interest, KanaeSelfState
        from services.kanae_self.recall import augment_query_with_interests
        state = KanaeSelfState(interests={
            "bird": Interest(topic="bird", salience=0.5, channel_id="dm-111"),
            "slidev": Interest(topic="slidev", salience=0.7, channel_id="server-222"),
        })
        # Without channel: slidev wins on raw salience.
        out_global = augment_query_with_interests("query", state, k=1)
        assert "slidev" in out_global
        # With channel dm-111: bird (0.5 + 0.5 = 1.0) wins over slidev (0.7).
        out_local = augment_query_with_interests(
            "query", state, k=1, channel_id="dm-111",
        )
        assert "bird" in out_local
        assert "slidev" not in out_local.split("| topics:")[1]

    def test_top_interest_topics_with_channel_id(self):
        from services.kanae_self.state import Interest, KanaeSelfState
        from services.kanae_self.recall import top_interest_topics
        state = KanaeSelfState(interests={
            "a": Interest(topic="a", salience=0.4, channel_id="111"),
            "b": Interest(topic="b", salience=0.85, channel_id="222"),
        })
        assert top_interest_topics(state, k=1, channel_id="111") == ["a"]
        assert top_interest_topics(state, k=1) == ["b"]

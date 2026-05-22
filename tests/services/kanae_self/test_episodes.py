"""Tests for services.kanae_self.episodes — ring buffer append + recent_episodes."""

from datetime import datetime, timedelta, timezone

import pytest

from services.kanae_self.episodes import append_episode, recent_episodes
from services.kanae_self.state import Episode, KanaeSelfState, MAX_EPISODES

JST = timezone(timedelta(hours=9))
FIXED_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=JST)


class TestAppendEpisode:
    def test_append_episode_basic(self):
        state = KanaeSelfState()
        ep = append_episode(state, summary="話した内容", now=FIXED_NOW)
        assert ep is not None
        assert ep.summary == "話した内容"
        assert ep.ts == FIXED_NOW.isoformat(timespec="seconds")
        assert len(state.episodes) == 1
        assert state.updated_at == ep.ts

    def test_append_empty_summary_returns_none(self):
        state = KanaeSelfState()
        result = append_episode(state, summary="", now=FIXED_NOW)
        assert result is None
        assert len(state.episodes) == 0

    def test_append_whitespace_summary_returns_none(self):
        state = KanaeSelfState()
        result = append_episode(state, summary="   ", now=FIXED_NOW)
        assert result is None
        assert len(state.episodes) == 0

    def test_append_truncates_summary(self):
        state = KanaeSelfState()
        long_summary = "x" * 200
        ep = append_episode(state, summary=long_summary, now=FIXED_NOW)
        assert ep is not None
        assert len(ep.summary) == 120
        assert ep.summary == "x" * 120

    def test_append_topics_normalized(self):
        state = KanaeSelfState()
        ep = append_episode(
            state, summary="test", topics=["UK Garage", " RUST "], now=FIXED_NOW
        )
        assert ep is not None
        assert ep.topics == ["uk garage", "rust"]

    def test_append_topics_max_3(self):
        state = KanaeSelfState()
        ep = append_episode(
            state,
            summary="test",
            topics=["a", "b", "c", "d", "e"],
            now=FIXED_NOW,
        )
        assert ep is not None
        assert ep.topics == ["a", "b", "c"]

    def test_append_valence_clamped(self):
        state = KanaeSelfState()
        ep_high = append_episode(
            state, summary="positive", valence_delta=5.0, now=FIXED_NOW
        )
        assert ep_high is not None
        assert ep_high.valence_delta == 1.0

        state2 = KanaeSelfState()
        ep_low = append_episode(
            state2, summary="negative", valence_delta=-5.0, now=FIXED_NOW
        )
        assert ep_low is not None
        assert ep_low.valence_delta == -1.0

    def test_append_importance_clamped(self):
        state = KanaeSelfState()
        ep_high = append_episode(
            state, summary="very important", importance=10, now=FIXED_NOW
        )
        assert ep_high is not None
        assert ep_high.importance == 5

        state2 = KanaeSelfState()
        ep_low = append_episode(
            state2, summary="low importance", importance=0, now=FIXED_NOW
        )
        assert ep_low is not None
        assert ep_low.importance == 1

    def test_append_ring_buffer_eviction(self):
        state = KanaeSelfState()
        for i in range(MAX_EPISODES):
            append_episode(state, summary=f"episode {i}", now=FIXED_NOW)
        assert len(state.episodes) == MAX_EPISODES
        first_summary = state.episodes[0].summary

        append_episode(state, summary="newest episode", now=FIXED_NOW)
        assert len(state.episodes) == MAX_EPISODES
        assert state.episodes[-1].summary == "newest episode"
        # oldest was evicted
        assert state.episodes[0].summary != first_summary

    def test_append_custom_episode_id(self):
        state = KanaeSelfState()
        ep = append_episode(
            state, summary="custom id test", episode_id="custom123", now=FIXED_NOW
        )
        assert ep is not None
        assert ep.id == "custom123"

    def test_append_auto_episode_id(self):
        state = KanaeSelfState()
        ep = append_episode(state, summary="auto id test", now=FIXED_NOW)
        assert ep is not None
        assert len(ep.id) == 8
        # all hex characters
        int(ep.id, 16)


class TestRecentEpisodes:
    def test_recent_episodes_basic(self):
        state = KanaeSelfState()
        for i in range(5):
            append_episode(state, summary=f"episode {i}", now=FIXED_NOW)
        result = recent_episodes(state, k=3)
        assert len(result) == 3
        assert result[-1].summary == "episode 4"
        assert result[0].summary == "episode 2"

    def test_recent_episodes_k_zero(self):
        state = KanaeSelfState()
        append_episode(state, summary="something", now=FIXED_NOW)
        result = recent_episodes(state, k=0)
        assert result == []

    def test_recent_episodes_empty_state(self):
        state = KanaeSelfState()
        result = recent_episodes(state, k=3)
        assert result == []

    def test_recent_episodes_k_larger_than_buffer(self):
        state = KanaeSelfState()
        append_episode(state, summary="ep 0", now=FIXED_NOW)
        append_episode(state, summary="ep 1", now=FIXED_NOW)
        result = recent_episodes(state, k=5)
        assert len(result) == 2


class TestRecentEpisodesChannelAware:
    def _make_state(self, episodes):
        from services.kanae_self.state import KanaeSelfState
        return KanaeSelfState(episodes=list(episodes))

    def test_no_channel_id_unchanged(self):
        from services.kanae_self.state import Episode
        from services.kanae_self.episodes import recent_episodes
        eps = [
            Episode(id=str(i), ts=f"2026-05-18T1{i}:00:00+09:00", channel_id="111")
            for i in range(5)
        ]
        state = self._make_state(eps)
        assert recent_episodes(state, k=3) == eps[-3:]

    def test_prefers_same_channel(self):
        from services.kanae_self.state import Episode
        from services.kanae_self.episodes import recent_episodes
        # Interleaved: A, B, A, B, A — newest at end
        eps = [
            Episode(id="1", ts="2026-05-18T10:00:00+09:00", channel_id="A"),
            Episode(id="2", ts="2026-05-18T11:00:00+09:00", channel_id="B"),
            Episode(id="3", ts="2026-05-18T12:00:00+09:00", channel_id="A"),
            Episode(id="4", ts="2026-05-18T13:00:00+09:00", channel_id="B"),
            Episode(id="5", ts="2026-05-18T14:00:00+09:00", channel_id="A"),
        ]
        state = self._make_state(eps)
        result = recent_episodes(state, k=2, channel_id="A")
        assert [e.id for e in result] == ["3", "5"]

    def test_backfills_with_others_when_short(self):
        from services.kanae_self.state import Episode
        from services.kanae_self.episodes import recent_episodes
        # 1 same-channel, 4 others
        eps = [
            Episode(id="1", ts="2026-05-18T10:00:00+09:00", channel_id="B"),
            Episode(id="2", ts="2026-05-18T11:00:00+09:00", channel_id="B"),
            Episode(id="3", ts="2026-05-18T12:00:00+09:00", channel_id="A"),  # same
            Episode(id="4", ts="2026-05-18T13:00:00+09:00", channel_id="B"),
            Episode(id="5", ts="2026-05-18T14:00:00+09:00", channel_id="B"),
        ]
        state = self._make_state(eps)
        result = recent_episodes(state, k=3, channel_id="A")
        # 1 same (id=3) + 2 most-recent others (id=4,5)
        assert [e.id for e in result] == ["4", "5", "3"]

    def test_only_others_when_no_same_channel(self):
        from services.kanae_self.state import Episode
        from services.kanae_self.episodes import recent_episodes
        eps = [
            Episode(id="1", ts="2026-05-18T10:00:00+09:00", channel_id="B"),
            Episode(id="2", ts="2026-05-18T11:00:00+09:00", channel_id="B"),
            Episode(id="3", ts="2026-05-18T12:00:00+09:00", channel_id="C"),
        ]
        state = self._make_state(eps)
        result = recent_episodes(state, k=2, channel_id="A")
        assert [e.id for e in result] == ["2", "3"]

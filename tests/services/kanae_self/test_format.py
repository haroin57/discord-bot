"""Tests for services.kanae_self.format — rendering prompt blocks."""

from datetime import datetime, timedelta, timezone

import pytest

from services.kanae_self.format import (
    _format_relative,
    _format_valence,
    _strip_outer_tag,
    format_episodes_block,
    format_interests_block,
    format_kanae_self_block,
)
from services.kanae_self.state import Episode, Interest, KanaeSelfState

JST = timezone(timedelta(hours=9))
FIXED_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=JST)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _ago(**kwargs) -> str:
    """Return ISO timestamp for FIXED_NOW minus a given timedelta."""
    return _iso(FIXED_NOW - timedelta(**kwargs))


class TestFormatRelative:
    def test_format_relative_just_now(self):
        ts = _ago(seconds=30)
        assert _format_relative(ts, FIXED_NOW) == "たった今"

    def test_format_relative_minutes(self):
        ts = _ago(minutes=5)
        assert _format_relative(ts, FIXED_NOW) == "5分前"

    def test_format_relative_hours(self):
        ts = _ago(hours=3)
        assert _format_relative(ts, FIXED_NOW) == "3時間前"

    def test_format_relative_yesterday(self):
        ts = _ago(hours=36)  # 1.5 days
        assert _format_relative(ts, FIXED_NOW) == "昨日"

    def test_format_relative_days(self):
        ts = _ago(days=3)
        assert _format_relative(ts, FIXED_NOW) == "3日前"

    def test_format_relative_invalid(self):
        assert _format_relative("", FIXED_NOW) == "不明"


class TestFormatValence:
    def test_format_valence_positive(self):
        assert _format_valence(0.6) == "好意+"

    def test_format_valence_neutral(self):
        assert _format_valence(0.0) == "中立"

    def test_format_valence_negative(self):
        assert _format_valence(-0.6) == "苦手-"


class TestFormatInterestsBlock:
    def test_format_interests_block_empty(self):
        state = KanaeSelfState()
        result = format_interests_block(state, now=FIXED_NOW)
        assert result == ""

    def test_format_interests_block_with_data(self):
        state = KanaeSelfState()
        state.interests = {
            "uk garage": Interest(
                topic="uk garage",
                salience=0.9,
                last_touched_at=_ago(minutes=10),
                mention_count=3,
                valence_attached=0.6,
            ),
        }
        result = format_interests_block(state, now=FIXED_NOW)
        assert result.startswith("<kanae_interests")
        assert "uk garage" in result
        assert "</kanae_interests>" in result


class TestFormatEpisodesBlock:
    def test_format_episodes_block_empty(self):
        state = KanaeSelfState()
        result = format_episodes_block(state, now=FIXED_NOW)
        assert result == ""

    def test_format_episodes_block_with_data(self):
        state = KanaeSelfState()
        state.episodes = [
            Episode(id="abc00001", ts=_ago(minutes=5), summary="会話の要約その1"),
            Episode(id="abc00002", ts=_ago(minutes=2), summary="会話の要約その2"),
        ]
        result = format_episodes_block(state, now=FIXED_NOW)
        assert result.startswith("<kanae_episodes")
        assert "会話の要約" in result
        assert "</kanae_episodes>" in result


class TestStripOuterTag:
    def test_strip_outer_tag(self):
        rendered = "<foo bar>content</foo>"
        assert _strip_outer_tag(rendered, "foo") == "content"

    def test_strip_outer_tag_empty(self):
        assert _strip_outer_tag("", "foo") == ""

    def test_strip_outer_tag_no_match(self):
        rendered = "no tags here"
        assert _strip_outer_tag(rendered, "foo") == "no tags here"


class TestFormatKanaeSelfBlock:
    def test_format_kanae_self_block_empty(self):
        result = format_kanae_self_block(
            self_state=None, emotion_block="", self_facts_block=""
        )
        assert result == ""

    def test_format_kanae_self_block_with_emotion(self):
        emotion_block = '<kanae_state note="test">mood: calm\narousal: 0.3</kanae_state>'
        result = format_kanae_self_block(
            self_state=None,
            emotion_block=emotion_block,
            self_facts_block="",
            now=FIXED_NOW,
        )
        assert result.startswith("<kanae_self")
        assert "<state>" in result
        assert "mood: calm" in result
        assert "</kanae_self>" in result

    def test_format_kanae_self_block_with_all(self):
        state = KanaeSelfState()
        state.interests = {
            "uk garage": Interest(
                topic="uk garage",
                salience=0.9,
                last_touched_at=_ago(minutes=10),
                mention_count=2,
                valence_attached=0.5,
            ),
        }
        state.episodes = [
            Episode(id="ep000001", ts=_ago(minutes=5), summary="最近の会話"),
        ]
        emotion_block = '<kanae_state note="x">calm</kanae_state>'
        self_facts_block = (
            "<kanae_self_memory>私はエンジニアです</kanae_self_memory>"
        )
        result = format_kanae_self_block(
            self_state=state,
            emotion_block=emotion_block,
            self_facts_block=self_facts_block,
            now=FIXED_NOW,
        )
        assert result.startswith("<kanae_self")
        assert "<state>" in result
        assert "<interests" in result
        assert "<recent_episodes" in result
        assert "<self_facts>" in result
        assert "</kanae_self>" in result

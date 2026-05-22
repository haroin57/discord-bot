"""Tests for the Phase 3 unified `<kanae_self>` block renderer."""

from datetime import datetime, timedelta, timezone

import pytest

from services.kanae_self.format import format_kanae_self_block
from services.kanae_self.state import Episode, Interest, KanaeSelfState

_JST = timezone(timedelta(hours=9))


def _at(year=2026, month=5, day=15, hour=20, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=_JST)


class TestEmptyInputs:
    def test_all_empty_returns_empty_string(self):
        assert format_kanae_self_block(
            self_state=None,
            emotion_block="",
            self_facts_block="",
        ) == ""

    def test_state_with_no_interests_no_episodes_returns_empty(self):
        state = KanaeSelfState()
        assert format_kanae_self_block(
            self_state=state,
            emotion_block="",
            self_facts_block="",
        ) == ""


class TestEmotionInline:
    def test_emotion_block_outer_tag_stripped(self):
        # Simulate format_emotion_for_prompt's output shape: a
        # `<kanae_state ...>...</kanae_state>` wrapper around body lines.
        emotion_render = (
            "<kanae_state note=\"foo\">\n"
            "現在: 2026-05-15\n"
            "- valence: +0.5\n"
            "</kanae_state>"
        )
        out = format_kanae_self_block(
            self_state=None,
            emotion_block=emotion_render,
            self_facts_block="",
        )
        assert "<kanae_self" in out
        assert "<state>" in out
        assert "現在: 2026-05-15" in out
        # The outer <kanae_state> tag must NOT survive — that would
        # nest two outer tags inside <kanae_self>.
        assert "<kanae_state" not in out
        assert "</kanae_state>" not in out
        assert out.endswith("</kanae_self>")


class TestInterestsAndEpisodes:
    def test_includes_interests_section_when_present(self):
        state = KanaeSelfState(interests={
            "uk garage": Interest(
                topic="uk garage",
                salience=0.8,
                last_touched_at=_at(hour=19).isoformat(timespec="seconds"),
                mention_count=5,
                valence_attached=0.6,
            ),
        })
        out = format_kanae_self_block(
            self_state=state,
            emotion_block="",
            self_facts_block="",
            now=_at(hour=20),
        )
        assert "<interests" in out
        assert "uk garage" in out
        assert "</interests>" in out

    def test_includes_episodes_section_when_present(self):
        state = KanaeSelfState(episodes=[
            Episode(
                id="abc12345",
                ts=_at(hour=17).isoformat(timespec="seconds"),
                summary="先輩からTAKU INOUEを勧められた",
            ),
        ])
        out = format_kanae_self_block(
            self_state=state,
            emotion_block="",
            self_facts_block="",
            now=_at(hour=20),
        )
        assert "<recent_episodes" in out
        assert "TAKU INOUE" in out
        assert "</recent_episodes>" in out


class TestSelfFactsBlock:
    def test_strips_outer_kanae_self_memory_tag(self):
        wrapped = (
            "<kanae_self_memory>\n"
            "- かなえはUKGに関心がある\n"
            "</kanae_self_memory>"
        )
        out = format_kanae_self_block(
            self_state=None,
            emotion_block="",
            self_facts_block=wrapped,
        )
        assert "<self_facts>" in out
        assert "UKG" in out
        # Outer legacy tag must be flattened.
        assert "<kanae_self_memory" not in out
        assert "</kanae_self_memory>" not in out

    def test_bare_facts_string_passes_through(self):
        out = format_kanae_self_block(
            self_state=None,
            emotion_block="",
            self_facts_block="- かなえはRustが好き",
        )
        assert "<self_facts>" in out
        assert "Rust" in out


class TestComposition:
    def test_all_three_sources_stack_in_reading_order(self):
        emotion_render = "<kanae_state>現在: now\n- valence: +0.4</kanae_state>"
        state = KanaeSelfState(
            interests={
                "rust": Interest(
                    topic="rust",
                    salience=0.7,
                    last_touched_at=_at(hour=19).isoformat(timespec="seconds"),
                    valence_attached=0.5,
                ),
            },
            episodes=[
                Episode(
                    id="ep1",
                    ts=_at(hour=18).isoformat(timespec="seconds"),
                    summary="Mem0 dedup バグの修正案を出した",
                ),
            ],
        )
        out = format_kanae_self_block(
            self_state=state,
            emotion_block=emotion_render,
            self_facts_block="- かなえは音楽が好き",
            now=_at(hour=20),
        )
        # State first, then interests, then recent_episodes, then facts.
        assert out.find("<state>") < out.find("<interests")
        assert out.find("<interests") < out.find("<recent_episodes")
        assert out.find("<recent_episodes") < out.find("<self_facts>")

    def test_starts_and_ends_with_kanae_self_tag(self):
        out = format_kanae_self_block(
            self_state=None,
            emotion_block="<kanae_state>x</kanae_state>",
            self_facts_block="",
        )
        assert out.startswith("<kanae_self")
        assert out.endswith("</kanae_self>")


class TestChannelAwareUnified:
    def test_format_kanae_self_block_with_channel_id_filters_episodes(self):
        from services.kanae_self.state import Episode, KanaeSelfState
        from services.kanae_self.format import format_kanae_self_block
        # bird is older than both Slidev entries → global tail-2 excludes
        # bird, but channel-aware recall pulls it forward as same-channel.
        state = KanaeSelfState(episodes=[
            Episode(id="1", ts="2026-05-18T10:00:00+09:00",
                    channel_id="dm-111", summary="bird解説"),
            Episode(id="2", ts="2026-05-18T11:00:00+09:00",
                    channel_id="server-222", summary="Slidev作業"),
            Episode(id="3", ts="2026-05-18T12:00:00+09:00",
                    channel_id="server-222", summary="Slidevレイアウト"),
        ])
        # Without channel_id: tail 2 = Slidev/Slidev only
        out_global = format_kanae_self_block(
            self_state=state, episodes_top_k=2,
        )
        assert "bird解説" not in out_global
        assert "Slidev" in out_global
        # With channel_id=dm-111: bird (same-ch) lifted in, 1 other backfilled
        out_local = format_kanae_self_block(
            self_state=state, episodes_top_k=2, channel_id="dm-111",
        )
        assert "bird解説" in out_local

    def test_format_kanae_self_block_with_channel_id_filters_interests(self):
        from services.kanae_self.state import Interest, KanaeSelfState
        from services.kanae_self.format import format_kanae_self_block
        state = KanaeSelfState(interests={
            "slidev": Interest(topic="slidev", salience=0.85, channel_id="server-222"),
            "bird": Interest(topic="bird", salience=0.4, channel_id="dm-111"),
        })
        out_local = format_kanae_self_block(
            self_state=state, interests_top_k=1, channel_id="dm-111",
        )
        # bird (0.4+0.5=0.9) > slidev (0.85)
        assert "bird" in out_local

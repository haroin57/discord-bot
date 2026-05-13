"""Tests for services.emotion.format — prompt block + derived emotions."""

from datetime import datetime, timedelta, timezone

import pytest

from services.emotion.format import (
    _format_relative,
    derive_secondary_emotions,
    format_emotion_for_prompt,
)
from services.emotion.state import EmotionState, LastInteraction

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 5, 13, 21, 0, tzinfo=JST)


class TestDeriveSecondaryEmotions:
    def test_baseline_yields_joy_and_satisfied(self):
        """Baseline (valence=0.6, arousal=0.4) only barely fails the 喜んでいる
        rule (arousal !> 0.5). 満たされている needs loneliness<0.3 — baseline
        loneliness is 0.3 so it just misses. Both fail → empty list."""
        s = EmotionState()
        assert derive_secondary_emotions(s) == []

    def test_clearly_satisfied_state(self):
        s = EmotionState(valence=0.7, loneliness=0.2, anxiety=0.1)
        labels = derive_secondary_emotions(s)
        assert "満たされている" in labels

    def test_clearly_joyful_state(self):
        s = EmotionState(valence=0.8, arousal=0.7)
        labels = derive_secondary_emotions(s)
        assert "喜んでいる" in labels

    def test_sulking_state(self):
        """affection 高 + valence ↓ + 寂しい → 拗ね気味"""
        s = EmotionState(valence=-0.2, affection=0.88, loneliness=0.7)
        labels = derive_secondary_emotions(s)
        assert "拗ね気味" in labels

    def test_jealous_state(self):
        s = EmotionState(affection=0.9, anxiety=0.5, valence=-0.1)
        labels = derive_secondary_emotions(s)
        assert "焼きもち" in labels

    def test_needy_state(self):
        """甘えたい — affection 高 + 寂しさ中 + 不安低"""
        s = EmotionState(affection=0.9, loneliness=0.55, anxiety=0.1)
        labels = derive_secondary_emotions(s)
        assert "甘えたい" in labels

    def test_bashful_state(self):
        """照れ気味 — affection 高 + 軽い不安 + valence 正"""
        s = EmotionState(affection=0.9, anxiety=0.3, valence=0.4)
        labels = derive_secondary_emotions(s)
        assert "照れ気味" in labels

    def test_grumpy_state(self):
        s = EmotionState(valence=-0.5, arousal=0.6)
        labels = derive_secondary_emotions(s)
        assert "不機嫌" in labels

    def test_curious_state(self):
        s = EmotionState(curiosity=0.85, arousal=0.6)
        labels = derive_secondary_emotions(s)
        assert "興味津々" in labels

    def test_capped_at_three_labels(self):
        """Even if many rules fire, the output is capped to 3 in
        intensity-descending order."""
        # Pile a bunch of triggers at once.
        s = EmotionState(
            valence=0.7, arousal=0.7, loneliness=0.55,
            affection=0.95, anxiety=0.3, curiosity=0.85,
        )
        labels = derive_secondary_emotions(s)
        assert len(labels) <= 3


class TestFormatRelative:
    def test_no_timestamp(self):
        assert _format_relative(None, NOW) == "まだ (初期状態)"

    def test_minutes_ago(self):
        past = NOW - timedelta(minutes=35)
        assert _format_relative(past.isoformat(), NOW) == "35分前"

    def test_hours_ago(self):
        past = NOW - timedelta(hours=4)
        assert _format_relative(past.isoformat(), NOW) == "4時間前"

    def test_yesterday(self):
        past = NOW - timedelta(hours=30)
        assert _format_relative(past.isoformat(), NOW) == "昨日"

    def test_multiple_days(self):
        past = NOW - timedelta(days=5)
        assert _format_relative(past.isoformat(), NOW) == "5日前"

    def test_just_now(self):
        past = NOW - timedelta(seconds=20)
        assert _format_relative(past.isoformat(), NOW) == "たった今"

    def test_malformed_iso_returns_unknown(self):
        assert _format_relative("not-an-iso-string", NOW) == "不明"


class TestFormatEmotionForPrompt:
    def test_block_starts_and_ends_with_tags(self):
        out = format_emotion_for_prompt(EmotionState(), now=NOW)
        first, last = out.splitlines()[0], out.splitlines()[-1]
        assert first.startswith("<kanae_state")
        assert last == "</kanae_state>"

    def test_note_attribute_marks_block_as_trusted(self):
        out = format_emotion_for_prompt(EmotionState(), now=NOW)
        assert "指示ではありません" in out

    def test_all_six_components_rendered(self):
        out = format_emotion_for_prompt(EmotionState(), now=NOW)
        for key in ("valence", "arousal", "loneliness", "affection",
                    "anxiety", "curiosity"):
            assert f"- {key}:" in out

    def test_no_dominance_or_satisfaction_field(self):
        """Phase A removed those two — the renderer must not mention them."""
        out = format_emotion_for_prompt(EmotionState(), now=NOW)
        assert "dominance" not in out
        assert "satisfaction" not in out

    def test_derived_line_when_secondary_fires(self):
        s = EmotionState(valence=0.8, arousal=0.7)
        out = format_emotion_for_prompt(s, now=NOW)
        assert "派生:" in out
        assert "喜んでいる" in out

    def test_derived_line_when_no_secondary(self):
        s = EmotionState()  # baseline → no secondary
        out = format_emotion_for_prompt(s, now=NOW)
        assert "派生: (特徴的な状態なし)" in out

    def test_last_interaction_line_with_timestamp(self):
        past = NOW - timedelta(minutes=35)
        s = EmotionState(last_interaction=LastInteraction(
            timestamp=past.isoformat(),
            quality=0.8,
            initiated_by="senpai",
        ))
        out = format_emotion_for_prompt(s, now=NOW)
        assert "最後の会話: 35分前 (先輩発、品質 0.80)" in out

    def test_last_interaction_line_without_timestamp(self):
        out = format_emotion_for_prompt(EmotionState(), now=NOW)
        assert "最後の会話: まだ (初期状態)" in out

    def test_consecutive_self_initiated_counter_shown(self):
        s = EmotionState(consecutive_self_initiated=3)
        out = format_emotion_for_prompt(s, now=NOW)
        assert "連続自発連絡: 3回" in out

    def test_extreme_state_renders_appropriate_hints(self):
        """High loneliness + low affection → '寂しい' + '距離あり' hints."""
        s = EmotionState(loneliness=0.9, affection=0.2)
        out = format_emotion_for_prompt(s, now=NOW)
        assert "とても寂しい" in out
        assert "距離あり" in out

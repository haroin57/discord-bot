"""Tests for services.emotion.feedback.schedule_feedback."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from services.emotion import feedback as feedback_mod
from services.emotion.feedback import schedule_feedback
from services.emotion.state import EmotionState, load_state, save_state


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def tmp_state_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _fake_event(event_type="long_good_chat", intensity=0.7):
    return {
        "event_type": event_type,
        "intensity": intensity,
        "topics": ["work", "memes"],
        "intimacy_level": 0.8,
        "senpai_engagement": 0.9,
        "conflict_detected": False,
    }


class TestScheduleFeedback:
    def test_applies_event_and_saves(self, tmp_state_dir):
        save_state(EmotionState(), tmp_state_dir / "kanae_emotion.json")

        with patch(
            "services.emotion.feedback.analyze_interaction",
            new=AsyncMock(return_value=_fake_event()),
        ):
            _run(schedule_feedback(
                user_text="hello senpai",
                assistant_text="やっほー先輩",
                state_dir=tmp_state_dir,
            ))

        new_state = load_state(tmp_state_dir / "kanae_emotion.json")
        assert new_state.last_interaction.timestamp  # set to now
        assert new_state.last_interaction.initiated_by == "senpai"
        assert "work" in new_state.last_interaction.topics

    def test_analyzer_failure_is_swallowed(self, tmp_state_dir):
        with patch(
            "services.emotion.feedback.analyze_interaction",
            new=AsyncMock(side_effect=RuntimeError("haiku down")),
        ):
            _run(schedule_feedback(
                user_text="x",
                assistant_text="y",
                state_dir=tmp_state_dir,
            ))
        # No state file created or modified; no exception escapes.
        assert not (tmp_state_dir / "kanae_emotion.json").exists()

    def test_event_topics_capped_at_five(self, tmp_state_dir):
        save_state(EmotionState(), tmp_state_dir / "kanae_emotion.json")
        many = {**_fake_event(), "topics": [f"t{i}" for i in range(10)]}
        with patch(
            "services.emotion.feedback.analyze_interaction",
            new=AsyncMock(return_value=many),
        ):
            _run(schedule_feedback(
                user_text="x",
                assistant_text="y",
                state_dir=tmp_state_dir,
            ))
        st = load_state(tmp_state_dir / "kanae_emotion.json")
        assert len(st.last_interaction.topics) == 5

    def test_save_error_does_not_propagate(self, tmp_state_dir):
        with patch(
            "services.emotion.feedback.analyze_interaction",
            new=AsyncMock(return_value=_fake_event()),
        ), patch(
            "services.emotion.feedback.save_state",
            side_effect=OSError("disk full"),
        ):
            _run(schedule_feedback(
                user_text="x",
                assistant_text="y",
                state_dir=tmp_state_dir,
            ))

    def test_lock_timeout_skips_silently(self, tmp_state_dir):
        # Pre-create kanae_emotion.json so apply_event has something to load.
        save_state(EmotionState(), tmp_state_dir / "kanae_emotion.json")

        # Force the lock to time out: replace emotion_lock with a sync ctx
        # manager that always raises TimeoutError.
        from contextlib import contextmanager

        @contextmanager
        def boom(*args, **kwargs):
            raise TimeoutError("locked by another process")
            yield  # unreachable; keeps function a generator

        with patch(
            "services.emotion.feedback.analyze_interaction",
            new=AsyncMock(return_value=_fake_event()),
        ), patch(
            "services.emotion.feedback.emotion_lock",
            new=boom,
        ):
            # Must not raise.
            _run(schedule_feedback(
                user_text="x",
                assistant_text="y",
                state_dir=tmp_state_dir,
            ))

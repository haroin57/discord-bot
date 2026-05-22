"""Tests for services.emotion.runtime — EmotionDaemonContext + EMOTION_CTX."""

import pytest

from services.emotion.runtime import EMOTION_CTX, EmotionDaemonContext


class FakeDaemon:
    def __init__(self, alive: bool = True) -> None:
        self.is_alive = alive


# ---------------------------------------------------------------------------
# EmotionDaemonContext
# ---------------------------------------------------------------------------

class TestEmotionDaemonContext:
    @pytest.fixture()
    def ctx(self) -> EmotionDaemonContext:
        """Fresh context for each test, isolated from the module singleton."""
        return EmotionDaemonContext()

    def test_initial_state_none(self, ctx):
        assert ctx.get() is None

    def test_set_and_get(self, ctx):
        daemon = FakeDaemon()
        ctx.set(daemon)
        assert ctx.get() is daemon

    def test_clear_daemon(self, ctx):
        ctx.set(FakeDaemon())
        ctx.set(None)
        assert ctx.get() is None

    def test_is_available_alive(self, ctx):
        ctx.set(FakeDaemon(alive=True))
        assert ctx.is_available() is True

    def test_is_available_dead(self, ctx):
        ctx.set(FakeDaemon(alive=False))
        assert ctx.is_available() is False

    def test_is_available_none(self, ctx):
        assert ctx.is_available() is False


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

class TestModuleSingleton:
    def test_module_singleton(self):
        assert isinstance(EMOTION_CTX, EmotionDaemonContext)

"""Tests for services.mem0.priming — Phase ④ session priming buffer."""

from __future__ import annotations

import pytest

from services.mem0 import priming


@pytest.fixture(autouse=True)
def _clear_priming_buffer():
    priming.clear()
    yield
    priming.clear()


class TestTouchAndGetPrimedEntities:
    def test_touch_then_get(self):
        priming.touch("u1", "ch1", [10, 20])
        assert priming.get_primed_entities("u1", "ch1") == {10, 20}

    def test_scoped_per_channel(self):
        priming.touch("u1", "ch1", [10])
        priming.touch("u1", "ch2", [99])
        assert priming.get_primed_entities("u1", "ch1") == {10}
        assert priming.get_primed_entities("u1", "ch2") == {99}

    def test_scoped_per_user(self):
        priming.touch("u1", "ch1", [10])
        priming.touch("u2", "ch1", [10])
        priming.clear("u2", "ch1")
        assert priming.get_primed_entities("u1", "ch1") == {10}
        assert priming.get_primed_entities("u2", "ch1") == set()

    def test_ignores_empty_input(self):
        priming.touch("u1", "ch1", [])
        priming.touch("", "ch1", [10])
        assert priming.get_primed_entities("u1", "ch1") == set()


class TestTtlAndEviction:
    def test_entries_expire_after_ttl(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(priming.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(priming, "_PRIMING_TTL_SEC", 60)

        priming.touch("u1", "ch1", [10])
        clock["t"] += 30
        assert priming.get_primed_entities("u1", "ch1") == {10}
        clock["t"] += 40  # total 70s elapsed > 60s TTL
        assert priming.get_primed_entities("u1", "ch1") == set()

    def test_max_entities_evicts_stalest_first(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(priming.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(priming, "_PRIMING_MAX_ENTITIES_PER_KEY", 3)

        for i in range(5):
            clock["t"] += 1
            priming.touch("u1", "ch1", [i])

        live = priming.get_primed_entities("u1", "ch1")
        assert live == {2, 3, 4}  # the 3 most-recently-touched survive


class TestClear:
    def test_clear_all(self):
        priming.touch("u1", "ch1", [1])
        priming.touch("u2", "ch1", [2])
        priming.clear()
        assert priming.get_primed_entities("u1", "ch1") == set()
        assert priming.get_primed_entities("u2", "ch1") == set()

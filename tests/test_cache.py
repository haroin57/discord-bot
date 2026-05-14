"""Tests for utils.cache.TTLLRUCache."""

from __future__ import annotations

import time

import pytest

from utils.cache import TTLLRUCache


def test_set_and_get_basic():
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=10, ttl_s=60.0)
    c.set("a", 1)
    assert c.get("a") == 1


def test_get_missing_returns_none():
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=10, ttl_s=60.0)
    assert c.get("missing") is None


def test_overwrite_replaces_value():
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=10, ttl_s=60.0)
    c.set("a", 1)
    c.set("a", 2)
    assert c.get("a") == 2


def test_ttl_expiry(monkeypatch):
    """get() returns None and drops the entry once past ttl."""
    base = 1000.0
    fake_now = {"t": base}
    monkeypatch.setattr(time, "monotonic", lambda: fake_now["t"])
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=10, ttl_s=5.0)
    c.set("a", 1)
    fake_now["t"] = base + 4.9
    assert c.get("a") == 1
    fake_now["t"] = base + 5.0
    assert c.get("a") is None
    assert len(c) == 0


def test_lru_eviction_on_overflow():
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=3, ttl_s=60.0)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    c.set("d", 4)
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3
    assert c.get("d") == 4


def test_get_touches_lru():
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=3, ttl_s=60.0)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    # touch a → it becomes MRU
    c.get("a")
    c.set("d", 4)
    # b should be evicted (now oldest), not a
    assert c.get("a") == 1
    assert c.get("b") is None
    assert c.get("c") == 3
    assert c.get("d") == 4


def test_contains_respects_ttl(monkeypatch):
    base = 1000.0
    fake_now = {"t": base}
    monkeypatch.setattr(time, "monotonic", lambda: fake_now["t"])
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=10, ttl_s=5.0)
    c.set("a", 1)
    assert "a" in c
    fake_now["t"] = base + 6.0
    assert "a" not in c


def test_pop_removes_and_returns():
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=10, ttl_s=60.0)
    c.set("a", 1)
    assert c.pop("a") == 1
    assert c.get("a") is None
    assert c.pop("a") is None


def test_clear():
    c: TTLLRUCache[str, int] = TTLLRUCache(maxsize=10, ttl_s=60.0)
    c.set("a", 1)
    c.set("b", 2)
    c.clear()
    assert len(c) == 0


def test_invalid_maxsize():
    with pytest.raises(ValueError):
        TTLLRUCache(maxsize=0, ttl_s=60.0)
    with pytest.raises(ValueError):
        TTLLRUCache(maxsize=-1, ttl_s=60.0)


def test_invalid_ttl():
    with pytest.raises(ValueError):
        TTLLRUCache(maxsize=10, ttl_s=0)
    with pytest.raises(ValueError):
        TTLLRUCache(maxsize=10, ttl_s=-1.0)


def test_tuple_keys():
    c: TTLLRUCache[tuple[int, int], str] = TTLLRUCache(maxsize=10, ttl_s=60.0)
    c.set((1, 2), "a")
    c.set((1, 3), "b")
    assert c.get((1, 2)) == "a"
    assert c.get((1, 3)) == "b"

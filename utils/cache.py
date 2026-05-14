"""In-memory TTL + LRU cache.

Generic over key + value types. Single-threaded usage assumed — the
Discord bot runs on one asyncio event loop, so no locking is needed.
If a cross-thread caller appears, wrap calls in their own lock.

API:
    cache = TTLLRUCache(maxsize=128, ttl_s=300.0)
    cache.set(key, value)
    value_or_None = cache.get(key)
    cache.pop(key)
    cache.clear()
    len(cache)

Eviction:
- get() returns None and drops the entry if it has aged past ttl_s.
- set() drops the oldest entry when the size would exceed maxsize.
- get() / set() bump the entry to MRU on hit.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

__all__ = ["TTLLRUCache"]


class TTLLRUCache(Generic[K, V]):
    """Bounded cache with per-entry TTL and LRU eviction.

    `maxsize` caps the number of entries. `ttl_s` is the per-entry
    lifetime in seconds, measured from set() via time.monotonic().
    """

    __slots__ = ("_store", "_maxsize", "_ttl_s")

    def __init__(self, *, maxsize: int = 128, ttl_s: float = 300.0) -> None:
        if maxsize <= 0:
            raise ValueError(f"maxsize must be positive, got {maxsize}")
        if ttl_s <= 0:
            raise ValueError(f"ttl_s must be positive, got {ttl_s}")
        self._store: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl_s = ttl_s

    def get(self, key: K) -> V | None:
        """Return the cached value, or None if missing / expired.

        On hit, the entry becomes MRU. On expiry it is removed.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        expiry, value = entry
        if time.monotonic() >= expiry:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        """Insert/replace `value` with a fresh TTL. Evicts LRU when over cap."""
        expiry = time.monotonic() + self._ttl_s
        self._store[key] = (expiry, value)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def pop(self, key: K) -> V | None:
        """Remove and return the cached value (or None)."""
        entry = self._store.pop(key, None)
        if entry is None:
            return None
        return entry[1]

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        entry = self._store.get(key)  # type: ignore[arg-type]
        if entry is None:
            return False
        expiry, _ = entry
        if time.monotonic() >= expiry:
            self._store.pop(key, None)  # type: ignore[arg-type]
            return False
        return True

"""Process-local replay anchors: keep the window start fixed while the cache is warm.

A saturated rolling window drops its oldest row on every turn, so messages[0]
changes and the provider re-creates the whole cached history (~145k tokens per
turn on 2026-10-07). While the channel's prompt cache is still warm, keep the
previous window start and append; re-anchor to the minimal tail when the
extra budget is exhausted or the cache is cold anyway (no extra creation).
Rows are never modified; a missing anchor (deletion, clear, age-out) falls
back to the plain tail.
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading
import time


class StableReplayWindow:
    def __init__(self, *, max_channels: int = 100, max_extra_bytes: int = 100_000,
                 warm_seconds: float = 3600, clock=time.monotonic):
        if max_channels < 1:
            raise ValueError("max_channels must be positive")
        if max_extra_bytes < 0:
            raise ValueError("max_extra_bytes must be nonnegative")
        self._anchors: OrderedDict[int, tuple[str, float]] = OrderedDict()
        self._max_channels = max_channels
        self._max_extra_bytes = max_extra_bytes
        self._warm_seconds = warm_seconds
        self._clock = clock
        self._lock = threading.Lock()

    @staticmethod
    def _serialize(row: dict) -> bytes:
        return json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")

    @classmethod
    def _fingerprint(cls, row: dict) -> str:
        return hashlib.sha256(cls._serialize(row)).hexdigest()

    def _extra_fits(self, candidate: list[dict], minimum_rows: int) -> bool:
        extra_bytes = 0
        for row in candidate[:-minimum_rows]:
            extra_bytes += len(self._serialize(row))
            if extra_bytes > self._max_extra_bytes:
                return False
        return True

    def select(self, channel_id: int, rows: list[dict], *, minimum_rows: int,
               extra_rows: int) -> list[dict]:
        if minimum_rows < 1 or extra_rows < 0:
            raise ValueError("invalid replay bounds")
        now = self._clock()
        with self._lock:
            if not rows:
                self._anchors.pop(channel_id, None)
                return []
            bounded = rows[-(minimum_rows + extra_rows + 1):]
            anchor = self._anchors.pop(channel_id, None)
            selected = bounded[-minimum_rows:]
            # A cold cache gains nothing from a longer window: re-anchor small.
            if extra_rows and anchor and now - anchor[1] < self._warm_seconds:
                for index, row in enumerate(bounded):
                    if self._fingerprint(row) == anchor[0]:
                        candidate = bounded[index:]
                        if len(candidate) <= minimum_rows + extra_rows and self._extra_fits(candidate, minimum_rows):
                            selected = candidate
                        break
            self._anchors[channel_id] = (self._fingerprint(selected[0]), now)
            while len(self._anchors) > self._max_channels:
                self._anchors.popitem(last=False)
            return selected

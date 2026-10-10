"""Bounded, process-local replay anchors for an experimental append-only window."""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading


class StableReplayWindow:
    def __init__(self, *, max_channels: int = 100, max_extra_bytes: int = 100_000):
        if max_channels < 1:
            raise ValueError("max_channels must be positive")
        if max_extra_bytes < 0:
            raise ValueError("max_extra_bytes must be nonnegative")
        self._anchors = OrderedDict()
        self._max_channels = max_channels
        self._max_extra_bytes = max_extra_bytes
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
        with self._lock:
            if not rows:
                self._anchors.pop(channel_id, None)
                return []
            bounded = rows[-(minimum_rows + extra_rows + 1):]
            anchor = self._anchors.pop(channel_id, None)
            selected = bounded[-minimum_rows:]
            if extra_rows and anchor:
                for index, row in enumerate(bounded):
                    if self._fingerprint(row) == anchor:
                        candidate = bounded[index:]
                        if len(candidate) <= minimum_rows + extra_rows and self._extra_fits(candidate, minimum_rows):
                            selected = candidate
                        break
            self._anchors[channel_id] = self._fingerprint(selected[0])
            while len(self._anchors) > self._max_channels:
                self._anchors.popitem(last=False)
            return selected

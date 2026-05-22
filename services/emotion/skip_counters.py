"""Lightweight skip-reason counter for emotion feedback diagnostics.

Each time `schedule_feedback()` decides not to update the emotion state,
it calls `inc(reason)` here. The counts are kept in-memory and flushed
to `.state/emotion_skip_counters.json` on every increment so they
survive bot restarts and can be eyeballed during incidents.

Reasons we currently track:
  - "daemon_not_registered"  — EMOTION_CTX returned None at call time
  - "lazy_respawn_failed"    — daemon.query() returned None despite alive
  - "validator_rejected"     — LLM output failed validator
  - "lock_timeout"           — state-file lock contended >5s
  - "exception"              — schedule_feedback caught an unexpected error
  - "ok"                     — successful update (incremented on success)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

_COUNTER_FILENAME = "emotion_skip_counters.json"
_lock = threading.Lock()
_counts: dict[str, int] = {}
_started_at: str = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _path(state_dir: str | os.PathLike) -> Path:
    return Path(state_dir) / _COUNTER_FILENAME


def inc(reason: str, state_dir: str | os.PathLike | None = None) -> None:
    """Increment a counter and persist to disk (best-effort)."""
    with _lock:
        _counts[reason] = _counts.get(reason, 0) + 1
        snapshot = {
            "started_at": _started_at,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "counts": dict(_counts),
        }
    if state_dir is None:
        return
    try:
        atomic_write_json(_path(state_dir), snapshot)
    except OSError as exc:
        log.debug("skip counter persist failed: %s", exc)

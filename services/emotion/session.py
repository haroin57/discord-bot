"""Session ID management for daily/emotion/summary Claude CLI sessions.

Stores fixed session IDs in `.state/emotion_sessions.json`. Each session
persists across cron invocations so Claude CLI can `--resume` and keep
context.  When a session is detected as broken (CLI returns non-zero with
--resume), the caller should call `reset_session()` to generate a fresh ID.

The bot's emotion daemon (silence tick) and `scripts/kanae_daily.py`
both touch this file concurrently — different session keys, but the
same dict on disk. Without coordination, one side's `load → mutate →
save` could overwrite the other side's freshly-reset key. We guard the
read-modify-write cycle with a dedicated flock (`emotion_sessions.lock`)
distinct from the emotion-state lock so they don't starve each other.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from services.emotion.lock import emotion_lock

log = logging.getLogger(__name__)

_FILENAME = "emotion_sessions.json"
_LOCK_FILENAME = "emotion_sessions.lock"
_LOCK_TIMEOUT_S = 5.0

# Session keys
DAILY = "daily"
EMOTION = "emotion"
SUMMARY = "summary"

_ALL_KEYS = (DAILY, EMOTION, SUMMARY)


def _sessions_path(state_dir: str | os.PathLike) -> Path:
    return Path(state_dir) / _FILENAME


def _lock_path(state_dir: str | os.PathLike) -> Path:
    return Path(state_dir) / _LOCK_FILENAME


def _load_raw(state_dir: str | os.PathLike) -> dict[str, str]:
    """Read the on-disk dict. Must be called while holding the flock."""
    p = _sessions_path(state_dir)
    data: dict[str, str] = {}
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = {k: str(v) for k, v in raw.items() if k in _ALL_KEYS and v}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("emotion_sessions load failed (%s); regenerating", exc)
    return data


def load_sessions(state_dir: str | os.PathLike) -> dict[str, str]:
    """Load session IDs from disk. Missing keys get fresh UUIDs."""
    try:
        with emotion_lock(_lock_path(state_dir), blocking=True, timeout=_LOCK_TIMEOUT_S):
            return _load_and_fill(state_dir)
    except TimeoutError:
        # Lock contention is rare; fall back to lock-free read so callers
        # don't crash. Worst case we briefly observe stale IDs, but the
        # next reset will reconverge.
        log.warning("emotion_sessions lock timed out; reading without lock")
        data = _load_raw(state_dir)
        for key in _ALL_KEYS:
            data.setdefault(key, str(uuid.uuid4()))
        return data


def _load_and_fill(state_dir: str | os.PathLike) -> dict[str, str]:
    """Load + fill any missing keys, persisting if changed. Caller holds lock."""
    data = _load_raw(state_dir)
    changed = False
    for key in _ALL_KEYS:
        if key not in data:
            data[key] = str(uuid.uuid4())
            changed = True
    if changed:
        _save(data, state_dir)
    return data


def reset_session(state_dir: str | os.PathLike, key: str) -> str:
    """Replace a broken session with a fresh UUID. Returns the new ID."""
    new_id = str(uuid.uuid4())
    try:
        with emotion_lock(_lock_path(state_dir), blocking=True, timeout=_LOCK_TIMEOUT_S):
            sessions = _load_and_fill(state_dir)
            sessions[key] = new_id
            _save(sessions, state_dir)
    except TimeoutError:
        # Best-effort: write without lock so we don't lose the reset entirely.
        log.warning("emotion_sessions lock timed out during reset_session(%s)", key)
        sessions = _load_raw(state_dir)
        for k in _ALL_KEYS:
            sessions.setdefault(k, str(uuid.uuid4()))
        sessions[key] = new_id
        _save(sessions, state_dir)
    log.info("emotion session %s reset to %s", key, new_id)
    return new_id


def _save(data: dict[str, str], state_dir: str | os.PathLike) -> None:
    p = _sessions_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    payload = json.dumps(data, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        fd = None
        os.replace(tmp, p)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

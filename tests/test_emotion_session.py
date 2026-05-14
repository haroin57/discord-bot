"""Tests for services.emotion.session — session ID file with flock.

Ensures concurrent reset_session calls from different processes don't
silently drop each other's updates (P1-5 race condition fix).
"""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from services.emotion.session import (
    DAILY,
    EMOTION,
    SUMMARY,
    _ALL_KEYS,
    load_sessions,
    reset_session,
)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path


def test_load_sessions_creates_fresh_keys(state_dir: Path) -> None:
    sessions = load_sessions(state_dir)
    assert set(sessions.keys()) == set(_ALL_KEYS)
    for v in sessions.values():
        uuid.UUID(v)  # raises if not a uuid


def test_load_sessions_persists(state_dir: Path) -> None:
    first = load_sessions(state_dir)
    second = load_sessions(state_dir)
    assert first == second


def test_reset_session_changes_only_target_key(state_dir: Path) -> None:
    original = load_sessions(state_dir)
    new_id = reset_session(state_dir, DAILY)
    after = load_sessions(state_dir)
    assert after[DAILY] == new_id
    assert after[DAILY] != original[DAILY]
    assert after[EMOTION] == original[EMOTION]
    assert after[SUMMARY] == original[SUMMARY]


# ---------- concurrency: simulate bot daemon vs kanae_daily race ----------

def _reset_worker(state_dir_str: str, key: str, count: int) -> None:
    """Reset `key` `count` times in a row. Used in multi-process race test."""
    state_dir = Path(state_dir_str)
    for _ in range(count):
        reset_session(state_dir, key)


def test_concurrent_resets_dont_lose_keys(state_dir: Path) -> None:
    """Spawn two processes resetting DAILY and EMOTION at once.

    Without flock, the read-modify-write pair could drop one process's
    update. With flock, every reset is visible at the end.
    """
    load_sessions(state_dir)  # initialize file

    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_reset_worker, args=(str(state_dir), DAILY, 5))
    p2 = ctx.Process(target=_reset_worker, args=(str(state_dir), EMOTION, 5))
    p1.start()
    p2.start()
    p1.join(timeout=20)
    p2.join(timeout=20)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    # Both keys still exist and are valid UUIDs (no key got dropped).
    final = load_sessions(state_dir)
    assert set(final.keys()) == set(_ALL_KEYS)
    for v in final.values():
        uuid.UUID(v)


def test_lock_file_created(state_dir: Path) -> None:
    load_sessions(state_dir)
    assert (state_dir / "emotion_sessions.lock").exists()
    assert (state_dir / "emotion_sessions.json").exists()


def test_corrupt_json_recovers(state_dir: Path) -> None:
    p = state_dir / "emotion_sessions.json"
    p.write_text("{not valid json", encoding="utf-8")
    sessions = load_sessions(state_dir)
    assert set(sessions.keys()) == set(_ALL_KEYS)
    # File was rewritten with valid JSON
    reloaded = json.loads(p.read_text(encoding="utf-8"))
    assert set(reloaded.keys()) == set(_ALL_KEYS)

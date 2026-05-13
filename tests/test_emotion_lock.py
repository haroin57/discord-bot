"""Tests for services.emotion.lock — shared flock for state RMW."""

import multiprocessing
import os
import time

import pytest

from services.emotion.lock import emotion_lock


def _hold_lock_in_child(lock_path, duration_seconds, ready_signal_path):
    """Subprocess entry point: acquire the lock blocking and hold it."""
    with emotion_lock(lock_path, blocking=False):
        # Tell the parent we're in.
        with open(ready_signal_path, "w") as f:
            f.write("ready")
        time.sleep(duration_seconds)


class TestEmotionLock:
    def test_non_blocking_acquires_when_free(self, tmp_path):
        lock = tmp_path / "x.lock"
        with emotion_lock(lock, blocking=False):
            assert lock.exists()

    def test_non_blocking_raises_on_contention(self, tmp_path):
        """A second non-blocking acquire while held → BlockingIOError."""
        lock = tmp_path / "x.lock"
        ready = tmp_path / "ready"
        proc = multiprocessing.Process(
            target=_hold_lock_in_child, args=(str(lock), 1.5, str(ready)),
        )
        proc.start()
        try:
            # Wait until the child has the lock.
            for _ in range(50):
                if ready.exists():
                    break
                time.sleep(0.02)
            assert ready.exists(), "child failed to acquire lock"

            with pytest.raises(BlockingIOError):
                with emotion_lock(lock, blocking=False):
                    pass
        finally:
            proc.join(timeout=3)
            if proc.is_alive():
                proc.terminate()

    def test_blocking_waits_then_acquires(self, tmp_path):
        """With blocking=True and a generous timeout, the lock is eventually
        acquired after the holder releases."""
        lock = tmp_path / "x.lock"
        ready = tmp_path / "ready"
        proc = multiprocessing.Process(
            target=_hold_lock_in_child, args=(str(lock), 0.3, str(ready)),
        )
        proc.start()
        try:
            for _ in range(50):
                if ready.exists():
                    break
                time.sleep(0.02)
            assert ready.exists()

            t0 = time.monotonic()
            with emotion_lock(lock, blocking=True, timeout=2.0, poll_interval=0.02):
                waited = time.monotonic() - t0
            assert 0.1 <= waited <= 1.5
        finally:
            proc.join(timeout=3)

    def test_blocking_times_out(self, tmp_path):
        """If the lock is never released within the timeout, raise TimeoutError."""
        lock = tmp_path / "x.lock"
        ready = tmp_path / "ready"
        proc = multiprocessing.Process(
            target=_hold_lock_in_child, args=(str(lock), 2.0, str(ready)),
        )
        proc.start()
        try:
            for _ in range(50):
                if ready.exists():
                    break
                time.sleep(0.02)
            assert ready.exists()

            with pytest.raises(TimeoutError):
                with emotion_lock(lock, blocking=True, timeout=0.3, poll_interval=0.02):
                    pass
        finally:
            proc.join(timeout=4)
            if proc.is_alive():
                proc.terminate()

    def test_lock_released_on_exception_in_body(self, tmp_path):
        """`with` body raises → lock must still be released so the next
        acquire succeeds without contention."""
        lock = tmp_path / "x.lock"
        try:
            with emotion_lock(lock, blocking=False):
                raise ValueError("boom")
        except ValueError:
            pass
        # Second acquire must succeed immediately.
        with emotion_lock(lock, blocking=False):
            pass

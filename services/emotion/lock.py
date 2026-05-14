"""Shared flock for emotion state read-modify-write.

Both the cron tick and bot.py's per-conversation hook need to update
`.state/kanae_emotion.json`. Without coordination, a concurrent
`load → mutate → save` pair from each side can silently drop one of the
updates (R1 in kanae-emotion-architecture.md). This module hands out an
advisory flock that both call sites can wrap their RMW cycle in.

Design choices:
  - **Non-blocking by default** for the bot hook. A 10s wait inside the
    Discord event loop would freeze unrelated message handling, so we
    prefer to skip an emotion update over blocking.
  - **Blocking with timeout** for the cron tick. The tick is a short-lived
    cron job and *should* wait briefly so it never ships stale state.
  - **Same flock file** as the tick's existing `kanae_ping.lock`? No —
    that lock is held for the entire tick (including a 30s Claude CLI
    call), which would starve the bot hook. We use a dedicated
    `kanae_emotion.lock` held only around state I/O.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path

LOCK_FILENAME = "kanae_emotion.lock"


@contextlib.contextmanager
def emotion_lock(
    lock_path: str | os.PathLike,
    *,
    blocking: bool = False,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
):
    """Acquire an exclusive flock on `lock_path` for the duration of the block.

    Raises `BlockingIOError` immediately when `blocking=False` and the lock
    is held by another process. With `blocking=True`, polls every
    `poll_interval` seconds until `timeout` seconds elapse, then raises
    `TimeoutError` — never blocks indefinitely so a stuck tick doesn't
    wedge the bot.
    """
    p = Path(lock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Open with mode 0o600 (umask permitting); a stale lock file is fine to
    # reuse, we only care about the kernel's flock state.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if blocking:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    # EAGAIN/EWOULDBLOCK from a busy lock — wait + retry.
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"emotion_lock: timed out after {timeout}s on {p}"
                        ) from None
                    time.sleep(poll_interval)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

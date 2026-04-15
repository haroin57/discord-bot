#!/usr/bin/env python3
"""Entry point — matches free-code entrypoints/ pattern."""

import atexit
import fcntl
import logging
import logging.handlers
import os
import queue
import sys

from config import BOT_DIR, DISCORD_TOKEN, STATE_DIR
from utils.attachments import cleanup_stale_attachments

_LOG_LEVEL = os.environ.get("BOT_LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_LOG_PATH = os.path.join(BOT_DIR, "bot.log")

# Hold a strong reference to the QueueListener so its background thread
# isn't GC'd and can be cleanly stopped at process exit.
_log_listener: logging.handlers.QueueListener | None = None


def _setup_logging() -> None:
    """Initialize root logger with QueueHandler-based async file output.

    IMPORTANT: This must only run when the bot is actually launched, never
    at module import time. Previously the file handler was attached at the
    top level of main.py, which meant any test that transitively imported
    `main` would start writing to the production `bot.log` (we observed
    `tests/test_coverage_improvements.py` mock tracebacks landing in the
    live log file). Wrapping the setup in a function plus the QueueHandler
    indirection also lifts the synchronous disk write off the asyncio event
    loop — log records are handed to a worker thread instead.
    """
    global _log_listener

    root_logger = logging.getLogger()
    root_logger.setLevel(_LOG_LEVEL)
    formatter = logging.Formatter(_LOG_FORMAT)

    log_queue: queue.Queue = queue.Queue(-1)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root_logger.addHandler(queue_handler)

    real_handlers: list[logging.Handler] = []
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    real_handlers.append(stream_handler)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        real_handlers.append(file_handler)
    except OSError:
        pass  # fall back to stderr-only if disk is unwritable

    listener = logging.handlers.QueueListener(
        log_queue, *real_handlers, respect_handler_level=True
    )
    listener.start()
    _log_listener = listener
    atexit.register(listener.stop)


PIDFILE = os.path.join(STATE_DIR, "bot.pid")
_pidfile_handle = None  # keep FD alive so flock persists for the process lifetime


def _acquire_pidlock() -> None:
    """Prevent duplicate bot processes via flock-based PID file (race-free)."""
    global _pidfile_handle
    fh = open(PIDFILE, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        old_pid = fh.read().strip()
        print(
            f"ERROR: Bot already running (PID {old_pid or '?'}). "
            f"Kill it first or remove {PIDFILE}"
        )
        sys.exit(1)
    # Lock acquired; rewrite our PID.
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _pidfile_handle = fh

    def _release():
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass
        try:
            if os.path.isfile(PIDFILE):
                os.unlink(PIDFILE)
        except OSError:
            pass

    atexit.register(_release)


def main():
    _setup_logging()

    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN is not set. Create a .env file with:")
        print("  DISCORD_TOKEN=your_discord_bot_token_here")
        raise SystemExit(1)

    _acquire_pidlock()
    cleanup_stale_attachments()

    # Import bot here to trigger setup and event registration
    from bot import bot
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

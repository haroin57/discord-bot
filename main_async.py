#!/usr/bin/env python3
"""Async entry point — runs Discord bot + Web API Socket.IO server together.

Uses bot.start() + asyncio.gather() instead of the blocking bot.run() in
main.py, so both coroutines share the same event loop.
"""

import asyncio
import atexit
import fcntl
import logging
import logging.handlers
import os
import queue
import sys

from config import BOT_DIR, DISCORD_TOKEN, STATE_DIR, WEB_API_PORT, validate_discord_token
# cleanup_stale_attachments is now scheduled from bot.on_ready (Phase G/D5).

_LOG_LEVEL = os.environ.get("BOT_LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_LOG_PATH = os.path.join(BOT_DIR, "bot.log")

_log_listener: logging.handlers.QueueListener | None = None


def _setup_logging() -> None:
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
        pass

    listener = logging.handlers.QueueListener(
        log_queue, *real_handlers, respect_handler_level=True
    )
    listener.start()
    _log_listener = listener
    atexit.register(listener.stop)


PIDFILE = os.path.join(STATE_DIR, "bot.pid")
_pidfile_handle = None


def _acquire_pidlock() -> None:
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


async def main() -> None:
    _setup_logging()

    try:
        validate_discord_token()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        print("  Create a .env file with: DISCORD_TOKEN=your_discord_bot_token_here")
        raise SystemExit(1) from e

    _acquire_pidlock()
    # cleanup_stale_attachments now scheduled from bot.on_ready (Phase G/D5).

    from bot import bot
    from web_api import WebAPI

    web_api = WebAPI(bot)

    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        web_api.start(port=WEB_API_PORT),
    )


if __name__ == "__main__":
    asyncio.run(main())

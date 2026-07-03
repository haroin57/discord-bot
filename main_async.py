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
import signal
import sys

from config import BOT_DIR, DISCORD_TOKEN, STATE_DIR, WEB_API_HOST, WEB_API_PORT, validate_discord_token
# cleanup_stale_attachments is now scheduled from bot.on_ready (Phase G/D5).

_LOG_LEVEL = os.environ.get("BOT_LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
# BOT_LOG_PATH override exists so test runs can NEVER write to the production
# bot.log, no matter which import path ends up calling _setup_logging().
# tests/conftest.py sets it to a tmp file before collection; we observed
# MagicMock tracebacks from pytest workers interleaved with live-bot lines
# (2026-07-03, P0.25-d monitor false positive).
_LOG_PATH = os.environ.get("BOT_LOG_PATH") or os.path.join(BOT_DIR, "bot.log")

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


log = logging.getLogger(__name__)


async def main() -> None:
    try:
        from services.structured_log import install_structured_logging
        install_structured_logging()
    except ImportError:
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

    # 機能2: restore cooldown/quota + session freshness/sticky persisted by the
    # previous run, so a restart continues conversations and keeps rate limits
    # instead of wiping all in-memory ledgers.
    import services.runtime_state as runtime_state
    runtime_state.load_all()
    # Safety net: if main_task dies by exception (we skip the graceful-shutdown
    # block below), still persist on interpreter exit. Synchronous + idempotent;
    # the loop is already stopped by then so _collect() can't race it.
    atexit.register(runtime_state.save_all_sync)

    web_api = WebAPI(bot)

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal(sig):
        log.warning("Received signal %s; initiating graceful shutdown", sig.name)
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except (NotImplementedError, RuntimeError):
            pass  # Windows / non-main-thread

    async def _run_services():
        await asyncio.gather(
            bot.start(DISCORD_TOKEN),
            web_api.start(port=WEB_API_PORT, host=WEB_API_HOST),
        )

    main_task = asyncio.create_task(_run_services())

    # 機能2: snapshot runtime state every 60s and once more at shutdown.
    flush_task = asyncio.create_task(runtime_state.periodic_flush(shutdown_event))

    shutdown_task = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        [main_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if shutdown_task in done:
        log.info("Graceful shutdown: cancelling main_task and killing Claude subprocesses")
        try:
            from services.claude_sdk import kill_orphan_claude_cli
            kill_orphan_claude_cli()
        except Exception as e:
            log.warning("kill_orphan_claude_cli failed: %s", e)
        try:
            await bot.close()
        except Exception:
            pass
        main_task.cancel()
        try:
            await main_task
        except (asyncio.CancelledError, Exception):
            pass
        # 機能2: shutdown_event is set, so periodic_flush does its final flush
        # and returns. Wait (bounded) so we don't exit before the write lands.
        try:
            await asyncio.wait_for(flush_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            runtime_state.save_all_sync()


if __name__ == "__main__":
    asyncio.run(main())

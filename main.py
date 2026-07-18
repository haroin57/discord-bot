#!/usr/bin/env python3
"""Entry point — matches free-code entrypoints/ pattern."""

import logging
import logging.handlers
import os
import queue

from services.runtime_bootstrap import (
    DiscordTokenError,
    PidFileLock,
    RootLifecycle,
    RuntimeBootstrap,
    RuntimeSettings,
)

_runtime_bootstrap = RuntimeBootstrap()
# cleanup_stale_attachments is now scheduled from bot.on_ready (Phase G/D5);
# main.py no longer needs the import.

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BOT_DIR, ".state")

_LOG_LEVEL = os.environ.get("BOT_LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
# See main_async.py: BOT_LOG_PATH lets tests redirect file logging away from
# the production bot.log unconditionally.
_LOG_PATH = os.environ.get("BOT_LOG_PATH") or os.path.join(BOT_DIR, "bot.log")

# Hold a strong reference to the QueueListener so its background thread
# isn't GC'd and can be cleanly stopped at process exit.
_log_listener: logging.handlers.QueueListener | None = None
_root_lifecycle: RootLifecycle | None = None
_QUEUE_HANDLER_OWNER = "discord-bot-root-queue-v1"


def _setup_logging(lifecycle: RootLifecycle | None = None) -> None:
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
    global _log_listener, _root_lifecycle

    owner = lifecycle or _root_lifecycle
    if owner is None:
        owner = RootLifecycle()
        owner.arm_atexit()
        _root_lifecycle = owner

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        if (
            getattr(existing, "_discord_bot_logging_owner", None)
            != _QUEUE_HANDLER_OWNER
        ):
            continue
        listener = getattr(existing, "_discord_bot_listener", None)
        downstream = getattr(existing, "_discord_bot_downstream", ())
        if listener is not None and getattr(listener, "_thread", None) is not None:
            root_logger.setLevel(_LOG_LEVEL)
            _log_listener = listener
            owner.own_logging(
                logger=root_logger,
                handler=existing,
                listener=listener,
                downstream=downstream,
            )
            return
        root_logger.removeHandler(existing)
        existing.close()
        for child in downstream:
            child.close()

    previous_level = root_logger.level
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
    setattr(queue_handler, "_discord_bot_logging_owner", _QUEUE_HANDLER_OWNER)
    setattr(queue_handler, "_discord_bot_previous_level", previous_level)
    setattr(queue_handler, "_discord_bot_listener", listener)
    setattr(queue_handler, "_discord_bot_downstream", tuple(real_handlers))
    _log_listener = listener
    owner.own_logging(
        logger=root_logger,
        handler=queue_handler,
        listener=listener,
        downstream=tuple(real_handlers),
    )


PIDFILE = os.path.join(STATE_DIR, "bot.pid")
_pidfile_handle: PidFileLock | None = None


def _acquire_pidlock(lifecycle: RootLifecycle | None = None) -> None:
    """Prevent duplicate bot processes via flock-based PID file (race-free)."""
    global _pidfile_handle, _root_lifecycle
    lock = PidFileLock(PIDFILE)
    try:
        lock.acquire()
    except BlockingIOError:
        print(f"ERROR: Bot already running (PID ?). Kill it first or remove {PIDFILE}")
        raise SystemExit(1) from None
    _pidfile_handle = lock

    def _release() -> None:
        global _pidfile_handle
        lock.close()
        if _pidfile_handle is lock:
            _pidfile_handle = None

    owner = lifecycle or _root_lifecycle
    if owner is None:
        owner = RootLifecycle()
        owner.arm_atexit()
        _root_lifecycle = owner
    owner.callback("pidfile", _release)


def _configure_runtime_paths(settings: RuntimeSettings) -> None:
    """Install initialized paths without importing config at module import."""
    global BOT_DIR, STATE_DIR, PIDFILE, _LOG_PATH
    BOT_DIR = settings.bot_dir
    STATE_DIR = settings.state_dir
    PIDFILE = os.path.join(settings.state_dir, "bot.pid")
    if not os.environ.get("BOT_LOG_PATH"):
        _LOG_PATH = os.path.join(settings.bot_dir, "bot.log")


def main():
    global _root_lifecycle
    lifecycle = RootLifecycle()
    lifecycle.arm_atexit()
    _root_lifecycle = lifecycle
    try:
        runtime_settings = _runtime_bootstrap.initialize()
    except DiscordTokenError as e:
        lifecycle.close()
        print(f"ERROR: {e}")
        print("  Create a .env file with: DISCORD_TOKEN=your_discord_bot_token_here")
        raise SystemExit(1) from e
    except BaseException:
        try:
            _runtime_bootstrap.shutdown_sync()
        except Exception:
            logging.getLogger(__name__).exception(
                "runtime cleanup failed after initialization error"
            )
        lifecycle.close()
        raise

    _configure_runtime_paths(runtime_settings)
    primary_error: BaseException | None = None
    try:
        _setup_logging()
        _acquire_pidlock()
        # cleanup_stale_attachments moved into bot.on_ready (Phase G/D5) so the
        # event loop is available; running it sync here blocked startup on a
        # large tmpdir.

        # Import bot here to trigger setup and event registration.
        from bot import bot

        bot.run(runtime_settings.discord_token)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        shutdown_error: Exception | None = None
        try:
            _runtime_bootstrap.shutdown_sync()
        except Exception as exc:
            shutdown_error = exc
            if primary_error is not None:
                logging.getLogger(__name__).exception(
                    "runtime cleanup failed while propagating the primary error"
                )
        lifecycle_error: Exception | None = None
        try:
            lifecycle.close()
        except Exception as exc:
            lifecycle_error = exc
            if primary_error is not None:
                logging.getLogger(__name__).exception(
                    "root lifecycle cleanup failed while propagating the primary error"
                )
        if primary_error is None:
            if shutdown_error is not None:
                raise shutdown_error
            if lifecycle_error is not None:
                raise lifecycle_error


if __name__ == "__main__":
    main()

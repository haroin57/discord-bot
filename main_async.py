#!/usr/bin/env python3
"""Async entry point — runs Discord bot + minimal /health server together.

Uses bot.start() + asyncio.gather() instead of the blocking bot.run() in
main.py, so both coroutines share the same event loop. The Web API / Socket.IO
UI was removed in Phase 1 (Cloudflare offload); only the tiny GET /health
endpoint on :3002 survives, served by services.health_server.HealthServer so
the external uptime Worker probe never breaks.
"""

import asyncio
import logging
import logging.handlers
import os
import queue
import signal
from typing import Any

from services.runtime_bootstrap import (
    DiscordTokenError,
    PidFileLock,
    RootLifecycle,
    RuntimeBootstrap,
    RuntimeSettings,
)

_runtime_bootstrap = RuntimeBootstrap()
# cleanup_stale_attachments is now scheduled from bot.on_ready (Phase G/D5).

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BOT_DIR, ".state")

_LOG_LEVEL = os.environ.get("BOT_LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
# BOT_LOG_PATH override exists so test runs can NEVER write to the production
# bot.log, no matter which import path ends up calling _setup_logging().
# tests/conftest.py sets it to a tmp file before collection; we observed
# MagicMock tracebacks from pytest workers interleaved with live-bot lines
# (2026-07-03, P0.25-d monitor false positive).
_LOG_PATH = os.environ.get("BOT_LOG_PATH") or os.path.join(BOT_DIR, "bot.log")

_log_listener: logging.handlers.QueueListener | None = None
_root_lifecycle: RootLifecycle | None = None
_QUEUE_HANDLER_OWNER = "discord-bot-root-queue-v1"


def _setup_logging(lifecycle: RootLifecycle | None = None) -> None:
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
        pass

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


log = logging.getLogger(__name__)


async def _close_root_lifecycle(lifecycle: RootLifecycle) -> None:
    async_close = getattr(lifecycle, "aclose", None)
    if async_close is None:
        lifecycle.close()
        return
    await async_close()


def _configure_runtime_paths(settings: RuntimeSettings) -> None:
    """Install initialized paths without importing config at module import."""
    global BOT_DIR, STATE_DIR, PIDFILE, _LOG_PATH
    BOT_DIR = settings.bot_dir
    STATE_DIR = settings.state_dir
    PIDFILE = os.path.join(settings.state_dir, "bot.pid")
    if not os.environ.get("BOT_LOG_PATH"):
        _LOG_PATH = os.path.join(settings.bot_dir, "bot.log")


async def _bootstrap_oauth() -> bool:
    """Load, refresh, and durably verify the serving OAuth credential."""
    from services import oauth_manager

    initialized = oauth_manager.init()
    if not initialized:
        log.error("OAuth bootstrap could not load the active credential")

    async def _active_is_ready() -> bool:
        try:
            token = await oauth_manager.ensure_valid_token()
        except oauth_manager.OAuthRefreshFailed as exc:
            log.error(
                "OAuth bootstrap refresh failed (account=%s, error=%s)",
                oauth_manager.get_active_account(),
                type(exc).__name__,
            )
            return False
        detail = oauth_manager.oauth_status_detail()
        return bool(token and detail.get("credential_ok"))

    if await _active_is_ready():
        log.info(
            "OAuth bootstrap ready (account=%s)",
            oauth_manager.get_active_account(),
        )
        return True

    original_account = oauth_manager.get_active_account()
    switched = False
    if original_account == "primary":
        switched = oauth_manager.failover_to_secondary("auth")
    else:
        switched = await oauth_manager.failback_to_primary("startup")
    if switched and await _active_is_ready():
        log.warning(
            "OAuth bootstrap recovered through account switch (%s -> %s)",
            original_account,
            oauth_manager.get_active_account(),
        )
        return True

    detail = oauth_manager.oauth_status_detail()
    log.error(
        "OAuth bootstrap remains degraded (account=%s, reason=%s)",
        oauth_manager.get_active_account(),
        detail.get("credential_reason") or "token_unavailable",
    )
    return False


async def main() -> None:
    global _root_lifecycle
    lifecycle = RootLifecycle()
    lifecycle.arm_atexit()
    _root_lifecycle = lifecycle
    try:
        runtime_settings = _runtime_bootstrap.initialize()
    except DiscordTokenError as e:
        await _close_root_lifecycle(lifecycle)
        print(f"ERROR: {e}")
        print("  Create a .env file with: DISCORD_TOKEN=your_discord_bot_token_here")
        raise SystemExit(1) from e
    except BaseException as initialization_error:
        cleanup_task = asyncio.create_task(_runtime_bootstrap.shutdown())
        initialization_cancel, cleanup_error = await _await_cleanup_task(
            cleanup_task,
            None,
        )
        try:
            await _close_root_lifecycle(lifecycle)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        if initialization_cancel is not None:
            raise initialization_cancel
        if cleanup_error is not None:
            log.error(
                "runtime cleanup failed after initialization error: %s", cleanup_error
            )
        raise initialization_error.with_traceback(initialization_error.__traceback__)

    _configure_runtime_paths(runtime_settings)
    bot = None
    runtime_state: Any | None = None
    shutdown_event: asyncio.Event | None = None
    main_task: asyncio.Task[None] | None = None
    flush_task: asyncio.Task | None = None
    shutdown_task: asyncio.Task[bool] | None = None
    primary_error: BaseException | None = None
    first_cancel: asyncio.CancelledError | None = None
    runtime_state_needs_fallback = {"value": False}

    try:
        try:
            from services.structured_log import install_structured_logging

            handler = install_structured_logging()
            lifecycle.own_logging(logger=logging.getLogger(), handler=handler)
        except ImportError:
            _setup_logging()

        _acquire_pidlock()
        # cleanup_stale_attachments now scheduled from bot.on_ready (Phase G/D5).

        from bot import bot as runtime_bot
        from services.health_server import HealthServer

        bot = runtime_bot

        # 機能2: restore cooldown/quota + session freshness/sticky persisted by
        # the previous run.
        import services.runtime_state as runtime_state_module

        runtime_state = runtime_state_module
        _runtime_bootstrap.restore_runtime_state()
        runtime_state_needs_fallback["value"] = True

        def _save_runtime_state_fallback() -> None:
            if runtime_state_needs_fallback["value"]:
                runtime_state.save_all_sync()
                runtime_state_needs_fallback["value"] = False

        lifecycle.callback("runtime_state", _save_runtime_state_fallback)

        health_server = HealthServer(bot)
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        def _handle_signal(sig):
            log.warning("Received signal %s; initiating graceful shutdown", sig.name)
            begin_drain = getattr(bot, "begin_drain", None)
            if callable(begin_drain):
                begin_drain()
            shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            lifecycle.add_signal_handler(loop, sig, _handle_signal, sig)

        async def _run_services() -> None:
            health_task = asyncio.create_task(
                health_server.start(
                    port=runtime_settings.web_api_port,
                    host=runtime_settings.web_api_host,
                ),
            )
            # Give aiohttp the first event-loop turn so /live can bind before a
            # network token refresh. Discord starts only after the bootstrap
            # attempt, preventing the first user request from becoming the
            # implicit readiness trigger.
            await asyncio.sleep(0)
            try:
                await _bootstrap_oauth()
            except Exception as exc:
                # Keep Discord available for the operator-driven /login repair
                # path. Readiness remains failed through the OAuth subsystem.
                log.exception(
                    "OAuth bootstrap failed unexpectedly; starting Discord "
                    "in degraded mode: %s",
                    type(exc).__name__,
                )
            bot_task = asyncio.create_task(
                bot.start(runtime_settings.discord_token),
            )
            try:
                done, _ = await asyncio.wait(
                    (bot_task, health_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for service_task in (bot_task, health_task):
                    if service_task in done:
                        await service_task
            finally:
                for service_task in (bot_task, health_task):
                    if not service_task.done():
                        service_task.cancel()
                await asyncio.gather(bot_task, health_task, return_exceptions=True)

        main_task = asyncio.create_task(_run_services())
        flush_task = asyncio.create_task(runtime_state.periodic_flush(shutdown_event))
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, _ = await asyncio.wait(
            [main_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if main_task in done:
            # Retrieve and propagate service exceptions before entering the
            # common cleanup path. A normal early return is cleaned up too.
            await main_task
        else:
            log.info("Graceful shutdown requested")
    except asyncio.CancelledError as exc:
        primary_error = exc
        first_cancel = exc
    except BaseException as exc:
        primary_error = exc
    finally:
        cleanup_task = asyncio.create_task(
            _cleanup_runtime(
                lifecycle=lifecycle,
                bootstrap=_runtime_bootstrap,
                bot=bot,
                runtime_state=runtime_state,
                runtime_state_needs_fallback=runtime_state_needs_fallback,
                shutdown_event=shutdown_event,
                main_task=main_task,
                flush_task=flush_task,
                shutdown_task=shutdown_task,
                suppress_service_error=primary_error is not None,
            ),
            name="root-runtime-cleanup",
        )
        first_cancel, cleanup_error = await _await_cleanup_task(
            cleanup_task,
            first_cancel,
        )

    if first_cancel is not None:
        if cleanup_error is not None:
            log.error(
                "runtime cleanup failed while propagating cancellation: %s",
                cleanup_error,
            )
        raise first_cancel
    if primary_error is not None:
        if cleanup_error is not None:
            log.error(
                "runtime cleanup failed while propagating the primary error: %s",
                cleanup_error,
            )
        raise primary_error.with_traceback(primary_error.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error


async def _await_cleanup_task(
    task: asyncio.Task[None],
    first_cancel: asyncio.CancelledError | None,
) -> tuple[asyncio.CancelledError | None, BaseException | None]:
    """Wait for one shielded cleanup task through any number of cancellations."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if first_cancel is None:
                first_cancel = exc
        except BaseException:
            if task.done():
                break
            raise
    try:
        task.result()
    except asyncio.CancelledError as exc:
        if first_cancel is None:
            first_cancel = exc
        elif exc is not first_cancel:
            _attach_cleanup_failure(first_cancel, exc, "root cleanup task cancellation")
    except BaseException as exc:
        if first_cancel is not None:
            _attach_cleanup_failure(first_cancel, exc, "root cleanup task")
        return first_cancel, exc
    return first_cancel, None


def _attach_cleanup_failure(
    cancellation: asyncio.CancelledError,
    cleanup_error: BaseException,
    scope: str,
) -> None:
    cancellation.add_note(
        f"{scope} failed with {type(cleanup_error).__name__}: {cleanup_error}"
    )
    if cleanup_error is not cancellation and cancellation.__context__ is None:
        cancellation.__context__ = cleanup_error


async def _cleanup_runtime(
    *,
    lifecycle: RootLifecycle,
    bootstrap: RuntimeBootstrap,
    bot: object | None,
    runtime_state: object | None,
    runtime_state_needs_fallback: dict[str, bool],
    shutdown_event: asyncio.Event | None,
    main_task: asyncio.Task[None] | None,
    flush_task: asyncio.Task[object] | None,
    shutdown_task: asyncio.Task[bool] | None,
    suppress_service_error: bool,
) -> None:
    """Complete all async and sync root cleanup from one owned task."""
    failures: list[tuple[str, Exception]] = []
    fatal_errors: list[tuple[str, BaseException]] = []
    first_cancel: asyncio.CancelledError | None = None
    if shutdown_event is not None:
        shutdown_event.set()

    if bot is not None:
        try:
            await bot.close()  # type: ignore[attr-defined]
        except asyncio.CancelledError as exc:
            first_cancel = first_cancel or exc
        except Exception as exc:
            failures.append(("bot", exc))
        except BaseException as exc:
            fatal_errors.append(("bot", exc))

    if main_task is not None:
        if not main_task.done():
            main_task.cancel()
        try:
            await main_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not suppress_service_error:
                failures.append(("services", exc))
        except BaseException as exc:
            fatal_errors.append(("services", exc))

    if flush_task is not None and runtime_state is not None:
        try:
            await asyncio.wait_for(flush_task, timeout=5)
            runtime_state_needs_fallback["value"] = False
        except asyncio.TimeoutError:
            try:
                runtime_state.save_all_sync()  # type: ignore[attr-defined]
                runtime_state_needs_fallback["value"] = False
            except asyncio.CancelledError as exc:
                first_cancel = first_cancel or exc
            except Exception as exc:
                failures.append(("runtime_state", exc))
            except BaseException as exc:
                fatal_errors.append(("runtime_state", exc))
        except asyncio.CancelledError as exc:
            first_cancel = first_cancel or exc
            try:
                runtime_state.save_all_sync()  # type: ignore[attr-defined]
                runtime_state_needs_fallback["value"] = False
            except asyncio.CancelledError as fallback_cancel:
                first_cancel = first_cancel or fallback_cancel
            except Exception as fallback_error:
                failures.append(("runtime_state", fallback_error))
            except BaseException as fallback_error:
                fatal_errors.append(("runtime_state", fallback_error))
        except Exception:
            try:
                runtime_state.save_all_sync()  # type: ignore[attr-defined]
                runtime_state_needs_fallback["value"] = False
            except asyncio.CancelledError as exc:
                first_cancel = first_cancel or exc
            except Exception as exc:
                failures.append(("runtime_state", exc))
            except BaseException as exc:
                fatal_errors.append(("runtime_state", exc))
        except BaseException as exc:
            fatal_errors.append(("runtime_state", exc))
            try:
                runtime_state.save_all_sync()  # type: ignore[attr-defined]
                runtime_state_needs_fallback["value"] = False
            except asyncio.CancelledError as fallback_cancel:
                first_cancel = first_cancel or fallback_cancel
            except Exception as fallback_error:
                failures.append(("runtime_state", fallback_error))
            except BaseException as fallback_error:
                fatal_errors.append(("runtime_state", fallback_error))

    if shutdown_task is not None:
        if not shutdown_task.done():
            shutdown_task.cancel()
        try:
            await shutdown_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            failures.append(("shutdown_wait", exc))
        except BaseException as exc:
            fatal_errors.append(("shutdown_wait", exc))

    try:
        await bootstrap.shutdown()
    except asyncio.CancelledError as exc:
        first_cancel = first_cancel or exc
    except Exception as exc:
        failures.append(("runtime", exc))
    except BaseException as exc:
        fatal_errors.append(("runtime", exc))
    try:
        await _close_root_lifecycle(lifecycle)
    except asyncio.CancelledError as exc:
        first_cancel = first_cancel or exc
    except Exception as exc:
        failures.append(("root", exc))
    except BaseException as exc:
        fatal_errors.append(("root", exc))
    if fatal_errors:
        fatal = fatal_errors[0][1]
        diagnostics = [
            *(f"{name}: {error}" for name, error in fatal_errors[1:]),
            *(f"{name}: {error}" for name, error in failures),
        ]
        if first_cancel is not None:
            diagnostics.append(f"cleanup cancellation: {first_cancel}")
        if diagnostics:
            fatal.add_note("async runtime cleanup failures: " + ", ".join(diagnostics))
        raise fatal
    if first_cancel is not None:
        diagnostics = [
            *(f"{name}: {error}" for name, error in failures),
        ]
        if diagnostics:
            first_cancel.add_note(
                "async runtime cleanup failures: " + ", ".join(diagnostics)
            )
        raise first_cancel
    if failures:
        summary = ", ".join(f"{name}: {error}" for name, error in failures)
        raise RuntimeError(f"async runtime cleanup failed ({summary})")


if __name__ == "__main__":
    asyncio.run(main())

"""Unit tests for main_async.py."""

import asyncio
import fcntl
import logging
import os
import sys
import tempfile
import types

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_main_async(monkeypatch, tmp_path, *, mock_oauth=True):
    """Import main_async with patched config paths and return the module."""
    import importlib

    # Patch config values before importing main_async
    import config as _config

    monkeypatch.setattr(_config, "BOT_DIR", str(tmp_path))
    monkeypatch.setattr(_config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(_config, "DISCORD_TOKEN", "fake-token")
    monkeypatch.setattr(_config, "WEB_API_PORT", 19999)

    # Clear cached module so module-level code reruns with patched paths
    if "main_async" in sys.modules:
        del sys.modules["main_async"]

    mod = importlib.import_module("main_async")
    if mock_oauth:

        async def _oauth_ready():
            return True

        monkeypatch.setattr(mod, "_bootstrap_oauth", _oauth_ready)
    return mod


# ---------------------------------------------------------------------------
# _setup_logging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def test_adds_queue_handler(self, monkeypatch, tmp_path):
        mod = _reload_main_async(monkeypatch, tmp_path)
        root = logging.getLogger()
        before = len(root.handlers)
        mod._log_listener = None  # reset
        mod._setup_logging()
        assert any(isinstance(h, logging.handlers.QueueHandler) for h in root.handlers)
        # cleanup
        if mod._log_listener:
            mod._log_listener.stop()
            mod._log_listener = None
        # remove added handlers to avoid polluting other tests
        root.handlers[:] = [
            h for h in root.handlers if not isinstance(h, logging.handlers.QueueHandler)
        ]

    def test_log_listener_started(self, monkeypatch, tmp_path):
        mod = _reload_main_async(monkeypatch, tmp_path)
        mod._log_listener = None
        mod._setup_logging()
        assert mod._log_listener is not None
        mod._log_listener.stop()
        mod._log_listener = None
        # cleanup
        root = logging.getLogger()
        root.handlers[:] = [
            h for h in root.handlers if not isinstance(h, logging.handlers.QueueHandler)
        ]

    def test_oserror_on_file_handler_is_ignored(self, monkeypatch, tmp_path):
        """If log file cannot be created, no exception should propagate."""
        mod = _reload_main_async(monkeypatch, tmp_path)
        # Point log path to an unwritable location
        monkeypatch.setattr(mod, "_LOG_PATH", "/nonexistent_dir/bot.log")
        mod._log_listener = None
        mod._setup_logging()  # must not raise
        if mod._log_listener:
            mod._log_listener.stop()
            mod._log_listener = None
        root = logging.getLogger()
        root.handlers[:] = [
            h for h in root.handlers if not isinstance(h, logging.handlers.QueueHandler)
        ]

    def test_reconfiguration_reuses_owned_handler_and_restores_root(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main_async(monkeypatch, tmp_path)
        root = logging.getLogger()
        original_level = root.level

        class ExternalHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.was_closed = False

            def close(self):
                self.was_closed = True
                super().close()

        external = ExternalHandler()
        root.addHandler(external)
        lifecycle = mod.RootLifecycle()
        try:
            mod._setup_logging(lifecycle)
            first_listener = mod._log_listener
            mod._setup_logging(lifecycle)
            owned = [
                handler
                for handler in root.handlers
                if getattr(handler, "_discord_bot_logging_owner", None)
                == mod._QUEUE_HANDLER_OWNER
            ]

            assert len(owned) == 1
            assert mod._log_listener is first_listener
            assert external in root.handlers
            assert external.was_closed is False

            lifecycle.close()
            assert owned[0] not in root.handlers
            assert external in root.handlers
            assert external.was_closed is False
            assert root.level == original_level
        finally:
            if not lifecycle.closed:
                lifecycle.close()
            root.removeHandler(external)
            external.close()


# ---------------------------------------------------------------------------
# _acquire_pidlock
# ---------------------------------------------------------------------------


class TestAcquirePidlock:
    def test_writes_pid_to_file(self, monkeypatch, tmp_path):
        mod = _reload_main_async(monkeypatch, tmp_path)
        pid_path = str(tmp_path / "bot.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)
        mod._acquire_pidlock()
        content = open(pid_path).read()
        assert content == str(os.getpid())
        # cleanup: release lock
        if mod._pidfile_handle:
            try:
                fcntl.flock(mod._pidfile_handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            mod._pidfile_handle.close()
            mod._pidfile_handle = None

    def test_exits_when_lock_already_held(self, monkeypatch, tmp_path):
        """Second call with lock already held should call sys.exit(1)."""
        mod = _reload_main_async(monkeypatch, tmp_path)
        pid_path = str(tmp_path / "bot2.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)

        # Acquire the lock externally
        fh = open(pid_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        exit_calls = []
        monkeypatch.setattr(sys, "exit", lambda code: exit_calls.append(code))

        # Suppress print
        monkeypatch.setattr("builtins.print", lambda *a, **kw: None)

        try:
            mod._acquire_pidlock()
        except SystemExit:
            exit_calls.append(1)

        assert exit_calls, "sys.exit should have been called"
        assert exit_calls[0] == 1

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    def test_release_callback_cleans_up(self, monkeypatch, tmp_path):
        """The _release atexit callback should unlock and remove the pidfile."""
        import atexit

        mod = _reload_main_async(monkeypatch, tmp_path)
        pid_path = str(tmp_path / "bot3.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)

        registered = []
        original_register = atexit.register

        def _capture_register(func, *args, **kwargs):
            registered.append(func)
            return original_register(func, *args, **kwargs)

        monkeypatch.setattr(atexit, "register", _capture_register)

        mod._acquire_pidlock()

        close_fn = next(
            (f for f in reversed(registered) if f.__name__ == "close"),
            None,
        )
        assert close_fn is not None, (
            "RootLifecycle.close should be registered with atexit"
        )

        close_fn()
        assert not os.path.isfile(pid_path)


# ---------------------------------------------------------------------------
# main() — async
# ---------------------------------------------------------------------------


class TestMainAsync:
    class _Bootstrap:
        def __init__(self, tmp_path, *, initialize_error=None):
            self.settings = types.SimpleNamespace(
                bot_dir=str(tmp_path),
                state_dir=str(tmp_path),
                discord_token="fake-token",
                web_api_port=19999,
                web_api_host="127.0.0.1",
            )
            self.initialize_error = initialize_error
            self.initialize_calls = 0
            self.restore_calls = 0
            self.shutdown_calls = 0

        def initialize(self):
            self.initialize_calls += 1
            if self.initialize_error is not None:
                raise self.initialize_error
            return self.settings

        def restore_runtime_state(self):
            self.restore_calls += 1

        async def shutdown(self):
            self.shutdown_calls += 1

    async def test_raises_system_exit_on_missing_token(self, monkeypatch, tmp_path):
        """Runtime settings validation failure should cause SystemExit(1)."""
        mod = _reload_main_async(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(
            tmp_path,
            initialize_error=mod.DiscordTokenError("token missing"),
        )
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr("builtins.print", lambda *a, **kw: None)

        with pytest.raises(SystemExit) as exc_info:
            await mod.main()
        assert exc_info.value.code == 1
        assert bootstrap.shutdown_calls == 0

    async def test_non_token_initialization_error_runs_shutdown(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main_async(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(
            tmp_path,
            initialize_error=OSError("state load failed"),
        )
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)

        with pytest.raises(OSError, match="state load failed"):
            await mod.main()

        assert bootstrap.shutdown_calls == 1

    async def test_repeated_cancellation_cannot_interrupt_owned_cleanup(self):
        import main_async as mod

        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def _cleanup():
            cleanup_started.set()
            await allow_cleanup.wait()
            cleanup_finished.set()

        async def _run():
            cleanup_task = asyncio.create_task(_cleanup())
            first_cancel, cleanup_error = await mod._await_cleanup_task(
                cleanup_task,
                None,
            )
            assert cleanup_error is None
            if first_cancel is not None:
                raise first_cancel

        task = asyncio.create_task(_run())
        await cleanup_started.wait()
        task.cancel("first-cancel")
        await asyncio.sleep(0)
        task.cancel("second-cancel")
        allow_cleanup.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task
        assert exc_info.value.args == ("first-cancel",)
        assert cleanup_finished.is_set()

    async def test_cleanup_dependency_cancellation_does_not_skip_later_cleanup(self):
        import main_async as mod

        calls = []
        first_cancel = asyncio.CancelledError("bot close cancelled")

        class Bot:
            async def close(self):
                calls.append("bot")
                raise first_cancel

        class Bootstrap:
            async def shutdown(self):
                calls.append("runtime")
                raise asyncio.CancelledError("runtime close cancelled")

        class Lifecycle:
            async def aclose(self):
                calls.append("root")
                raise KeyboardInterrupt("root callback failed")

        with pytest.raises(KeyboardInterrupt) as caught:
            await mod._cleanup_runtime(
                lifecycle=Lifecycle(),
                bootstrap=Bootstrap(),
                bot=Bot(),
                runtime_state=None,
                runtime_state_needs_fallback={"value": False},
                shutdown_event=None,
                main_task=None,
                flush_task=None,
                shutdown_task=None,
                suppress_service_error=False,
            )

        assert str(caught.value) == "root callback failed"
        assert calls == ["bot", "runtime", "root"]
        assert "bot close cancelled" in "\n".join(caught.value.__notes__)

    async def test_await_cleanup_task_prioritizes_outer_cancel_over_baseexception(
        self,
    ):
        import main_async as mod

        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        class CleanupFatal(BaseException):
            pass

        fatal = CleanupFatal("cleanup fatal")

        async def cleanup():
            cleanup_started.set()
            await release_cleanup.wait()
            raise fatal

        async def run_cleanup():
            cleanup_task = asyncio.create_task(cleanup())
            first_cancel, cleanup_error = await mod._await_cleanup_task(
                cleanup_task,
                None,
            )
            assert cleanup_error is fatal
            if first_cancel is not None:
                raise first_cancel

        runner = asyncio.create_task(run_cleanup())
        await cleanup_started.wait()
        runner.cancel("first outer cancellation")
        await asyncio.sleep(0)
        runner.cancel("second outer cancellation")
        release_cleanup.set()

        with pytest.raises(asyncio.CancelledError) as caught:
            await runner

        assert caught.value.args == ("first outer cancellation",)
        assert caught.value.__context__ is fatal
        assert "CleanupFatal: cleanup fatal" in "\n".join(caught.value.__notes__)

    async def test_calls_gather_when_token_valid(self, monkeypatch, tmp_path):
        """main() should call asyncio.gather with bot.start and health_server.start."""
        mod = _reload_main_async(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)

        # Suppress logging setup and pidlock side-effects
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        gathered = []

        async def _fake_bot_start(token):
            gathered.append(("bot.start", token))

        # Build fake bot and HealthServer
        async def _fake_bot_close():
            gathered.append(("bot.close", None))

        fake_bot = types.SimpleNamespace(start=_fake_bot_start, close=_fake_bot_close)

        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = fake_bot

        class FakeHealthServer:
            def __init__(self, _bot):
                pass

            async def start(self, port, host=None):
                gathered.append(("health_server.start", port))

        fake_health_module = types.ModuleType("services.health_server")
        fake_health_module.HealthServer = FakeHealthServer

        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)
        monkeypatch.setitem(sys.modules, "services.health_server", fake_health_module)

        await mod.main()

        assert any(k == "health_server.start" for k, _ in gathered)
        assert any(k == "bot.close" for k, _ in gathered)
        assert bootstrap.shutdown_calls == 1

    async def test_oauth_bootstrap_refreshes_active_credential_at_startup(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main_async(monkeypatch, tmp_path, mock_oauth=False)
        from services import oauth_manager

        calls = []
        monkeypatch.setattr(oauth_manager, "init", lambda: calls.append("init") or True)

        async def _ensure():
            calls.append("refresh")
            return "access-token"

        monkeypatch.setattr(oauth_manager, "ensure_valid_token", _ensure)
        monkeypatch.setattr(
            oauth_manager,
            "oauth_status_detail",
            lambda: {"credential_ok": True},
        )
        monkeypatch.setattr(oauth_manager, "get_active_account", lambda: "primary")

        assert await mod._bootstrap_oauth() is True
        assert calls == ["init", "refresh"]

    async def test_oauth_bootstrap_uses_standby_when_primary_is_not_ready(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main_async(monkeypatch, tmp_path, mock_oauth=False)
        from services import oauth_manager

        active = {"name": "primary"}
        attempts = []
        monkeypatch.setattr(oauth_manager, "init", lambda: True)

        async def _ensure():
            attempts.append(active["name"])
            return None if active["name"] == "primary" else "standby-token"

        async def _failover(_reason):
            active["name"] = "failover"
            return True

        monkeypatch.setattr(oauth_manager, "ensure_valid_token", _ensure)
        monkeypatch.setattr(oauth_manager, "get_active_account", lambda: active["name"])
        monkeypatch.setattr(
            oauth_manager,
            "prepare_and_failover_to_secondary",
            _failover,
        )
        monkeypatch.setattr(
            oauth_manager,
            "oauth_status_detail",
            lambda: {"credential_ok": active["name"] == "failover"},
        )

        assert await mod._bootstrap_oauth() is True
        assert attempts == ["primary", "failover"]

    async def test_oauth_bootstrap_restores_primary_when_active_failover_is_dead(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main_async(monkeypatch, tmp_path, mock_oauth=False)
        from services import oauth_manager

        active = {"name": "failover"}
        attempts = []
        monkeypatch.setattr(oauth_manager, "init", lambda: True)

        async def _ensure():
            attempts.append(active["name"])
            if active["name"] == "failover":
                raise oauth_manager.OAuthRefreshFailed("invalid_grant")
            return "primary-token"

        async def _failback(_reason):
            active["name"] = "primary"
            return True

        monkeypatch.setattr(oauth_manager, "ensure_valid_token", _ensure)
        monkeypatch.setattr(oauth_manager, "get_active_account", lambda: active["name"])
        monkeypatch.setattr(oauth_manager, "failback_to_primary", _failback)
        monkeypatch.setattr(
            oauth_manager,
            "oauth_status_detail",
            lambda: {"credential_ok": active["name"] == "primary"},
        )

        assert await mod._bootstrap_oauth() is True
        assert attempts == ["failover", "primary"]
        assert active["name"] == "primary"

    async def test_settings_initialized_before_services(self, monkeypatch, tmp_path):
        mod = _reload_main_async(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        calls = []

        async def _noop(*a, **kw):
            calls.append("service")

        async def _close():
            calls.append("close")

        fake_bot = types.SimpleNamespace(start=_noop, close=_close)
        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = fake_bot

        class FakeHealthServer:
            def __init__(self, _b):
                pass

            async def start(self, port, host=None):
                pass

        fake_health_module = types.ModuleType("services.health_server")
        fake_health_module.HealthServer = FakeHealthServer

        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)
        monkeypatch.setitem(sys.modules, "services.health_server", fake_health_module)

        await mod.main()
        assert bootstrap.initialize_calls == 1
        assert calls.count("service") == 1
        assert bootstrap.shutdown_calls == 1

    async def test_health_starts_before_oauth_and_discord_waits_for_bootstrap(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main_async(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)
        health_started = asyncio.Event()
        events = []

        async def _oauth_bootstrap():
            assert health_started.is_set()
            events.append("oauth")
            return True

        async def _bot_start(_token):
            events.append("discord")

        async def _bot_close():
            return None

        monkeypatch.setattr(mod, "_bootstrap_oauth", _oauth_bootstrap)
        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = types.SimpleNamespace(
            start=_bot_start,
            close=_bot_close,
        )

        class FakeHealthServer:
            def __init__(self, _bot):
                pass

            async def start(self, port, host=None):
                events.append("health")
                health_started.set()
                await asyncio.Future()

        fake_health_module = types.ModuleType("services.health_server")
        fake_health_module.HealthServer = FakeHealthServer
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)
        monkeypatch.setitem(sys.modules, "services.health_server", fake_health_module)

        await mod.main()

        assert events[:3] == ["health", "oauth", "discord"]

    async def test_main_task_failure_is_retrieved_and_cleanup_runs(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main_async(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)
        close_calls = []

        async def _start(_token):
            raise OSError("discord start failed")

        async def _close():
            close_calls.append(True)

        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = types.SimpleNamespace(start=_start, close=_close)

        class FakeHealthServer:
            def __init__(self, _bot):
                pass

            async def start(self, port, host=None):
                await asyncio.sleep(9999)

        fake_health_module = types.ModuleType("services.health_server")
        fake_health_module.HealthServer = FakeHealthServer
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)
        monkeypatch.setitem(sys.modules, "services.health_server", fake_health_module)

        with pytest.raises(OSError, match="discord start failed"):
            await mod.main()

        assert close_calls == [True]
        assert bootstrap.shutdown_calls == 1

    @pytest.mark.parametrize("winner", ["bot", "health"])
    async def test_normal_service_exit_cancels_and_awaits_peer(
        self,
        monkeypatch,
        tmp_path,
        winner,
    ):
        mod = _reload_main_async(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)
        bot_started = asyncio.Event()
        health_started = asyncio.Event()
        bot_cancelled = asyncio.Event()
        health_cancelled = asyncio.Event()

        async def _bot_start(_token):
            bot_started.set()
            if winner == "bot":
                await health_started.wait()
                return
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                bot_cancelled.set()
                raise

        async def _bot_close():
            return None

        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = types.SimpleNamespace(
            start=_bot_start,
            close=_bot_close,
        )

        class FakeHealthServer:
            def __init__(self, _bot):
                pass

            async def start(self, port, host=None):
                health_started.set()
                if winner == "health":
                    await bot_started.wait()
                    return
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    health_cancelled.set()
                    raise

        fake_health_module = types.ModuleType("services.health_server")
        fake_health_module.HealthServer = FakeHealthServer
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)
        monkeypatch.setitem(sys.modules, "services.health_server", fake_health_module)

        await asyncio.wait_for(mod.main(), timeout=1)

        assert bot_started.is_set()
        assert health_started.is_set()
        assert bot_cancelled.is_set() is (winner == "health")
        assert health_cancelled.is_set() is (winner == "bot")
        assert bootstrap.shutdown_calls == 1


# ---------------------------------------------------------------------------
# SIGTERM graceful shutdown
# ---------------------------------------------------------------------------


class TestSigtermHandler:
    async def test_sigterm_triggers_common_cleanup(self, monkeypatch, tmp_path):
        """A signal uses the same provider/session cleanup as other exits."""
        import signal as _signal

        mod = _reload_main_async(monkeypatch, tmp_path)
        bootstrap = TestMainAsync._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)

        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        fake_bot_close_calls = []
        lifecycle_calls = []

        async def _bot_start(token):
            await asyncio.sleep(9999)

        async def _bot_close():
            lifecycle_calls.append("close")
            fake_bot_close_calls.append(True)

        def _begin_drain():
            lifecycle_calls.append("begin_drain")

        fake_bot = types.SimpleNamespace(
            start=_bot_start,
            close=_bot_close,
            begin_drain=_begin_drain,
        )
        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = fake_bot

        class FakeHealthServer:
            def __init__(self, _b):
                pass

            async def start(self, port, host=None):
                await asyncio.sleep(9999)

        fake_health_module = types.ModuleType("services.health_server")
        fake_health_module.HealthServer = FakeHealthServer

        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)
        monkeypatch.setitem(sys.modules, "services.health_server", fake_health_module)
        registered_handlers = {}

        def fake_add_signal_handler(sig, cb, *args):
            registered_handlers[sig] = (cb, args)

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal_handler)

        async def _run():
            task = asyncio.create_task(mod.main())
            # Yield to let main() register signal handlers
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if _signal.SIGTERM in registered_handlers:
                cb, args = registered_handlers[_signal.SIGTERM]
                cb(*args)
            await task

        await _run()

        assert fake_bot_close_calls == [True]
        assert lifecycle_calls[:2] == ["begin_drain", "close"]
        assert bootstrap.shutdown_calls == 1


# ---------------------------------------------------------------------------
# Module-level __main__ guard
# ---------------------------------------------------------------------------


class TestMainGuard:
    def test_module_level_constants_exist(self, monkeypatch, tmp_path):
        """PIDFILE and _log_listener should exist at module level."""
        mod = _reload_main_async(monkeypatch, tmp_path)
        assert hasattr(mod, "PIDFILE")
        assert hasattr(mod, "_log_listener")

    def test_log_level_from_env(self, monkeypatch, tmp_path):
        """_LOG_LEVEL should reflect BOT_LOG_LEVEL env var."""
        monkeypatch.setenv("BOT_LOG_LEVEL", "DEBUG")
        mod = _reload_main_async(monkeypatch, tmp_path)
        assert mod._LOG_LEVEL == "DEBUG"
        monkeypatch.delenv("BOT_LOG_LEVEL", raising=False)

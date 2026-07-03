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

def _reload_main_async(monkeypatch, tmp_path):
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
        root.handlers[:] = [h for h in root.handlers if not isinstance(h, logging.handlers.QueueHandler)]

    def test_log_listener_started(self, monkeypatch, tmp_path):
        mod = _reload_main_async(monkeypatch, tmp_path)
        mod._log_listener = None
        mod._setup_logging()
        assert mod._log_listener is not None
        mod._log_listener.stop()
        mod._log_listener = None
        # cleanup
        root = logging.getLogger()
        root.handlers[:] = [h for h in root.handlers if not isinstance(h, logging.handlers.QueueHandler)]

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
        root.handlers[:] = [h for h in root.handlers if not isinstance(h, logging.handlers.QueueHandler)]


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

        # Find and call _release (last registered callback that is not _log_listener.stop)
        release_fn = next(
            (f for f in reversed(registered) if f.__name__ == "_release"),
            None,
        )
        assert release_fn is not None, "_release should be registered with atexit"

        # Call it — should not raise and should remove the pidfile
        release_fn()
        assert not os.path.isfile(pid_path)


# ---------------------------------------------------------------------------
# main() — async
# ---------------------------------------------------------------------------

class TestMainAsync:
    async def test_raises_system_exit_on_missing_token(self, monkeypatch, tmp_path):
        """validate_discord_token raising RuntimeError should cause SystemExit(1)."""
        mod = _reload_main_async(monkeypatch, tmp_path)

        def _bad_validate():
            raise RuntimeError("token missing")

        # main_async imports validate_discord_token at module level via `from config import`
        # so we must patch the name on the module itself.
        monkeypatch.setattr(mod, "validate_discord_token", _bad_validate)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr("builtins.print", lambda *a, **kw: None)

        with pytest.raises(SystemExit) as exc_info:
            await mod.main()
        assert exc_info.value.code == 1

    async def test_calls_gather_when_token_valid(self, monkeypatch, tmp_path):
        """main() should call asyncio.gather with bot.start and health_server.start."""
        mod = _reload_main_async(monkeypatch, tmp_path)

        # Suppress logging setup and pidlock side-effects
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        gathered = []

        async def _fake_bot_start(token):
            gathered.append(("bot.start", token))

        # Build fake bot and HealthServer
        fake_bot = types.SimpleNamespace(start=_fake_bot_start)

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

        # validate_discord_token returns normally
        monkeypatch.setattr(mod, "validate_discord_token", lambda: "fake-token")
        monkeypatch.setattr(mod, "DISCORD_TOKEN", "fake-token")
        monkeypatch.setattr(mod, "WEB_API_PORT", 19999)

        await mod.main()

        assert any(k == "health_server.start" for k, _ in gathered)

    async def test_validate_called_before_gather(self, monkeypatch, tmp_path):
        """validate_discord_token must be called during main()."""
        mod = _reload_main_async(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        calls = []

        def _validate():
            calls.append("validate")
            return "fake-token"

        monkeypatch.setattr(mod, "validate_discord_token", _validate)
        monkeypatch.setattr(mod, "DISCORD_TOKEN", "fake-token")
        monkeypatch.setattr(mod, "WEB_API_PORT", 19999)

        async def _noop(*a, **kw):
            pass

        fake_bot = types.SimpleNamespace(start=_noop)
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
        assert "validate" in calls


# ---------------------------------------------------------------------------
# SIGTERM graceful shutdown
# ---------------------------------------------------------------------------

class TestSigtermHandler:
    async def test_sigterm_triggers_kill_orphan(self, monkeypatch, tmp_path):
        """When shutdown_event fires, kill_orphan_claude_cli must be called."""
        import signal as _signal

        mod = _reload_main_async(monkeypatch, tmp_path)

        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)
        monkeypatch.setattr(mod, "validate_discord_token", lambda: "fake-token")
        monkeypatch.setattr(mod, "DISCORD_TOKEN", "fake-token")
        monkeypatch.setattr(mod, "WEB_API_PORT", 19999)

        kill_calls = []
        fake_bot_close_calls = []

        async def _bot_start(token):
            await asyncio.sleep(9999)

        async def _bot_close():
            fake_bot_close_calls.append(True)

        fake_bot = types.SimpleNamespace(start=_bot_start, close=_bot_close)
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
        # main_async's shutdown imports kill_orphan_claude_cli from
        # services.claude_api (PR9: claude_sdk deleted); patch the attribute.
        import services.claude_api as _capi
        monkeypatch.setattr(
            _capi, "kill_orphan_claude_cli", lambda: kill_calls.append(True))

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

        assert kill_calls, "kill_orphan_claude_cli should have been called on SIGTERM"


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

"""Unit tests for main.py (sync entry point)."""

import atexit
import fcntl
import importlib
import logging
import os
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_main(monkeypatch, tmp_path):
    """Import main with patched config paths and return the module."""
    import config as _config
    monkeypatch.setattr(_config, "BOT_DIR", str(tmp_path))
    monkeypatch.setattr(_config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(_config, "DISCORD_TOKEN", "fake-token")

    if "main" in sys.modules:
        del sys.modules["main"]

    mod = importlib.import_module("main")
    return mod


# ---------------------------------------------------------------------------
# _setup_logging
# ---------------------------------------------------------------------------

class TestSetupLogging:
    def _cleanup(self, mod):
        if mod._log_listener:
            mod._log_listener.stop()
            mod._log_listener = None
        root = logging.getLogger()
        root.handlers[:] = [
            h for h in root.handlers
            if not isinstance(h, logging.handlers.QueueHandler)
        ]

    def test_adds_queue_handler(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        mod._log_listener = None
        mod._setup_logging()
        root = logging.getLogger()
        assert any(isinstance(h, logging.handlers.QueueHandler) for h in root.handlers)
        self._cleanup(mod)

    def test_log_listener_started(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        mod._log_listener = None
        mod._setup_logging()
        assert mod._log_listener is not None
        self._cleanup(mod)

    def test_oserror_on_file_handler_is_ignored(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_LOG_PATH", "/nonexistent_dir/bot.log")
        mod._log_listener = None
        mod._setup_logging()  # must not raise
        self._cleanup(mod)


# ---------------------------------------------------------------------------
# _acquire_pidlock
# ---------------------------------------------------------------------------

class TestAcquirePidlock:
    def test_writes_pid_to_file(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        pid_path = str(tmp_path / "bot.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)
        mod._acquire_pidlock()
        content = open(pid_path).read()
        assert content == str(os.getpid())
        if mod._pidfile_handle:
            try:
                fcntl.flock(mod._pidfile_handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            mod._pidfile_handle.close()
            mod._pidfile_handle = None

    def test_exits_when_lock_already_held(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        pid_path = str(tmp_path / "bot2.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)

        fh = open(pid_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        exit_calls = []
        monkeypatch.setattr(sys, "exit", lambda code: exit_calls.append(code))
        monkeypatch.setattr("builtins.print", lambda *a, **kw: None)

        try:
            mod._acquire_pidlock()
        except SystemExit:
            exit_calls.append(1)

        assert exit_calls, "sys.exit should have been called"
        assert exit_calls[0] == 1

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    def test_pidfile_handle_is_stored(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        pid_path = str(tmp_path / "bot3.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)
        mod._acquire_pidlock()
        assert mod._pidfile_handle is not None
        if mod._pidfile_handle:
            try:
                fcntl.flock(mod._pidfile_handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            mod._pidfile_handle.close()
            mod._pidfile_handle = None

    def test_release_closure_runs_via_atexit(self, monkeypatch, tmp_path):
        """Trigger the _release closure registered in atexit to cover lines 95-107."""
        mod = _reload_main(monkeypatch, tmp_path)
        pid_path = str(tmp_path / "bot4.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)

        # Capture atexit callbacks registered during _acquire_pidlock
        registered = []
        original_register = atexit.register

        def _capture_register(fn, *args, **kwargs):
            registered.append((fn, args, kwargs))
            return original_register(fn, *args, **kwargs)

        monkeypatch.setattr(atexit, "register", _capture_register)
        mod._acquire_pidlock()

        # The _release closure is the last atexit callback registered
        release_callbacks = [(fn, a, kw) for fn, a, kw in registered if fn.__name__ == "_release"]
        assert release_callbacks, "Expected _release atexit callback"

        release_fn = release_callbacks[-1][0]
        # Call it directly — exercises lines 95-107
        release_fn()

        # Pidfile should be removed
        assert not os.path.isfile(pid_path)

    def test_release_closure_handles_already_closed_fh(self, monkeypatch, tmp_path):
        """_release should not raise when fh is already closed."""
        mod = _reload_main(monkeypatch, tmp_path)
        pid_path = str(tmp_path / "bot5.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)

        registered = []
        original_register = atexit.register

        def _capture_register(fn, *args, **kwargs):
            registered.append((fn, args, kwargs))
            return original_register(fn, *args, **kwargs)

        monkeypatch.setattr(atexit, "register", _capture_register)
        mod._acquire_pidlock()

        # Close fh before calling _release to exercise exception suppression
        if mod._pidfile_handle:
            mod._pidfile_handle.close()

        release_callbacks = [(fn, a, kw) for fn, a, kw in registered if fn.__name__ == "_release"]
        release_fn = release_callbacks[-1][0]
        release_fn()  # must not raise


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def _make_fake_bot_module(self):
        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = types.SimpleNamespace(run=lambda token: None)
        return fake_bot_module

    def test_raises_system_exit_on_invalid_token(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "validate_discord_token", lambda: (_ for _ in ()).throw(RuntimeError("bad token")))
        monkeypatch.setattr("builtins.print", lambda *a, **kw: None)

        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 1

    def test_validate_called_in_main(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        calls = []

        def _validate():
            calls.append("validate")

        monkeypatch.setattr(mod, "validate_discord_token", _validate)

        fake_bot_module = self._make_fake_bot_module()
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

        mod.main()
        assert "validate" in calls

    def test_bot_run_called_with_token(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)
        monkeypatch.setattr(mod, "validate_discord_token", lambda: None)
        monkeypatch.setattr(mod, "DISCORD_TOKEN", "my-fake-token")

        run_calls = []

        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = types.SimpleNamespace(run=lambda token: run_calls.append(token))
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

        mod.main()
        assert run_calls == ["my-fake-token"]

    def test_acquire_pidlock_called_before_bot_run(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "validate_discord_token", lambda: None)

        order = []
        pid_path = str(tmp_path / "order.pid")
        monkeypatch.setattr(mod, "PIDFILE", pid_path)
        monkeypatch.setattr(mod, "_pidfile_handle", None)

        orig_acquire = mod._acquire_pidlock

        def _tracked_acquire():
            order.append("pidlock")
            orig_acquire()

        monkeypatch.setattr(mod, "_acquire_pidlock", _tracked_acquire)

        fake_bot_module = types.ModuleType("bot")

        def _run(token):
            order.append("bot.run")

        fake_bot_module.bot = types.SimpleNamespace(run=_run)
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

        mod.main()
        assert order.index("pidlock") < order.index("bot.run")

        if mod._pidfile_handle:
            try:
                fcntl.flock(mod._pidfile_handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            mod._pidfile_handle.close()
            mod._pidfile_handle = None

    def test_setup_logging_called_first(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        order = []

        def _logging():
            order.append("logging")

        def _validate():
            order.append("validate")

        monkeypatch.setattr(mod, "_setup_logging", _logging)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)
        monkeypatch.setattr(mod, "validate_discord_token", _validate)

        fake_bot_module = self._make_fake_bot_module()
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

        mod.main()
        assert order.index("logging") < order.index("validate")


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

class TestModuleConstants:
    def test_pidfile_and_log_listener_exist(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        assert hasattr(mod, "PIDFILE")
        assert hasattr(mod, "_log_listener")

    def test_log_level_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BOT_LOG_LEVEL", "DEBUG")
        mod = _reload_main(monkeypatch, tmp_path)
        assert mod._LOG_LEVEL == "DEBUG"
        monkeypatch.delenv("BOT_LOG_LEVEL", raising=False)

    def test_log_level_default_info(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BOT_LOG_LEVEL", raising=False)
        mod = _reload_main(monkeypatch, tmp_path)
        assert mod._LOG_LEVEL == "INFO"

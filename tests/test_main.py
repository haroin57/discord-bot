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
            h for h in root.handlers if not isinstance(h, logging.handlers.QueueHandler)
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

    def test_reconfiguration_reuses_owned_handler_and_restores_root(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main(monkeypatch, tmp_path)
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

        close_callbacks = [
            (fn, a, kw) for fn, a, kw in registered if fn.__name__ == "close"
        ]
        assert close_callbacks, "Expected RootLifecycle.close atexit callback"
        close_callbacks[-1][0]()

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

        close_callbacks = [
            (fn, a, kw) for fn, a, kw in registered if fn.__name__ == "close"
        ]
        close_callbacks[-1][0]()  # must not raise

    def test_symlink_pidfile_is_rejected_without_truncating_target(
        self,
        monkeypatch,
        tmp_path,
    ):
        mod = _reload_main(monkeypatch, tmp_path)
        victim = tmp_path / "victim"
        victim.write_text("keep", encoding="utf-8")
        pid_path = tmp_path / "bot-symlink.pid"
        pid_path.symlink_to(victim)
        monkeypatch.setattr(mod, "PIDFILE", str(pid_path))

        with pytest.raises(OSError):
            mod._acquire_pidlock()

        assert victim.read_text(encoding="utf-8") == "keep"

    def test_release_does_not_unlink_replacement_path(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        pid_path = tmp_path / "bot-replaced.pid"
        monkeypatch.setattr(mod, "PIDFILE", str(pid_path))
        mod._acquire_pidlock()
        owner = mod._root_lifecycle
        assert owner is not None

        pid_path.unlink()
        pid_path.write_text("replacement", encoding="utf-8")
        owner.close()

        assert pid_path.read_text(encoding="utf-8") == "replacement"


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    class _Bootstrap:
        def __init__(self, tmp_path, *, token="fake-token", initialize_error=None):
            self.settings = types.SimpleNamespace(
                bot_dir=str(tmp_path),
                state_dir=str(tmp_path),
                discord_token=token,
            )
            self.initialize_error = initialize_error
            self.initialize_calls = 0
            self.shutdown_calls = 0

        def initialize(self):
            self.initialize_calls += 1
            if self.initialize_error is not None:
                raise self.initialize_error
            return self.settings

        def shutdown_sync(self):
            self.shutdown_calls += 1

    def _make_fake_bot_module(self):
        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = types.SimpleNamespace(run=lambda token: None)
        return fake_bot_module

    def test_raises_system_exit_on_invalid_token(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(
            tmp_path,
            initialize_error=mod.DiscordTokenError("bad token"),
        )
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr("builtins.print", lambda *a, **kw: None)

        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 1
        assert bootstrap.shutdown_calls == 0

    def test_non_token_initialization_error_runs_shutdown(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(
            tmp_path,
            initialize_error=OSError("state load failed"),
        )
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)

        with pytest.raises(OSError, match="state load failed"):
            mod.main()

        assert bootstrap.shutdown_calls == 1

    def test_settings_initialized_in_main(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        fake_bot_module = self._make_fake_bot_module()
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

        mod.main()
        assert bootstrap.initialize_calls == 1
        assert bootstrap.shutdown_calls == 1

    def test_bot_run_called_with_token(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path, token="my-fake-token")
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        run_calls = []

        fake_bot_module = types.ModuleType("bot")
        fake_bot_module.bot = types.SimpleNamespace(
            run=lambda token: run_calls.append(token)
        )
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

        mod.main()
        assert run_calls == ["my-fake-token"]
        assert bootstrap.shutdown_calls == 1

    def test_bot_exception_still_runs_shutdown(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        fake_bot_module = types.ModuleType("bot")

        def _raise(_token):
            raise OSError("discord failed")

        fake_bot_module.bot = types.SimpleNamespace(run=_raise)
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

        with pytest.raises(OSError, match="discord failed"):
            mod.main()
        assert bootstrap.shutdown_calls == 1

    def test_acquire_pidlock_called_before_bot_run(self, monkeypatch, tmp_path):
        mod = _reload_main(monkeypatch, tmp_path)
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        monkeypatch.setattr(mod, "_setup_logging", lambda: None)

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
        bootstrap = self._Bootstrap(tmp_path)
        monkeypatch.setattr(mod, "_runtime_bootstrap", bootstrap)
        order = []

        def _logging():
            order.append("logging")

        monkeypatch.setattr(mod, "_setup_logging", _logging)
        monkeypatch.setattr(mod, "_acquire_pidlock", lambda: None)

        fake_bot_module = self._make_fake_bot_module()
        monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

        mod.main()
        assert order == ["logging"]


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

"""RuntimeBootstrap and immutable settings contracts."""

from __future__ import annotations

import asyncio
import os
import logging
import signal
import subprocess
import sys
import importlib
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from services.runtime_bootstrap import (
    BootstrapState,
    PidFileLock,
    RootLifecycle,
    RuntimeBootstrap,
    RuntimeConfigurationError,
    RuntimeShutdownError,
    build_runtime_settings,
)


def _set_runtime_env(monkeypatch, tmp_path) -> dict[str, Path]:
    state = tmp_path / "state"
    paths = {
        "STATE_DIR": state,
        "HISTORY_DIR": state / "history",
        "SESSIONS_PATH": state / "sessions.json",
        "RUNTIME_STATE_PATH": state / "runtime_state.json",
        "ATTACHMENT_DIR": tmp_path / "attachments",
        "MERMAID_OUTPUT_DIR": tmp_path / "mermaid",
        "TABLE_RENDERS_DIR": tmp_path / "table",
        "MATH_RENDERS_DIR": tmp_path / "math",
    }
    monkeypatch.setenv("DISCORD_TOKEN", "runtime-token")
    monkeypatch.setenv("BOT_STATE_DIR", str(paths["STATE_DIR"]))
    monkeypatch.setenv("BOT_ATTACHMENT_DIR", str(paths["ATTACHMENT_DIR"]))
    for name in ("MERMAID_OUTPUT_DIR", "TABLE_RENDERS_DIR", "MATH_RENDERS_DIR"):
        monkeypatch.setenv(name, str(paths[name]))
    return paths


def _initialized_bootstrap(monkeypatch, tmp_path) -> RuntimeBootstrap:
    import config
    import services.session as session

    paths = _set_runtime_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        config,
        "load_system_prompt",
        lambda *, bot_dir=None: "runtime prompt",
    )
    monkeypatch.setattr(session, "SESSIONS_PATH", str(paths["SESSIONS_PATH"]))
    monkeypatch.setattr(session, "initialize_session_registry", lambda: None)
    bootstrap = RuntimeBootstrap(
        bot_dir=str(tmp_path),
        dotenv_path=str(tmp_path / "missing.env"),
    )
    bootstrap.initialize()
    return bootstrap


def test_config_import_does_not_create_runtime_directories(tmp_path):
    state = tmp_path / "state"
    attachment = tmp_path / "attachments"
    mermaid = tmp_path / "mermaid"
    table = tmp_path / "table"
    math = tmp_path / "math"
    env = os.environ.copy()
    env.update(
        {
            "BOT_STATE_DIR": str(state),
            "BOT_ATTACHMENT_DIR": str(attachment),
            "MERMAID_OUTPUT_DIR": str(mermaid),
            "TABLE_RENDERS_DIR": str(table),
            "MATH_RENDERS_DIR": str(math),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
    )

    assert not state.exists()
    assert not attachment.exists()
    assert not mermaid.exists()
    assert not table.exists()
    assert not math.exists()


def test_bootstrap_creates_runtime_paths_and_returns_frozen_settings(
    tmp_path,
    monkeypatch,
):
    import config
    import services.session as session

    paths = _set_runtime_env(monkeypatch, tmp_path)
    monkeypatch.setattr(session, "SESSIONS_PATH", str(paths["SESSIONS_PATH"]))
    monkeypatch.setattr(
        config,
        "load_system_prompt",
        lambda *, bot_dir=None: "runtime prompt",
    )
    session.reset_session_registry_for_tests()
    initialize_calls = []
    original_initialize = session.initialize_session_registry

    def _initialize_registry(*, force=False):
        initialize_calls.append(force)
        return original_initialize(force=force)

    monkeypatch.setattr(session, "initialize_session_registry", _initialize_registry)

    bootstrap = RuntimeBootstrap(
        bot_dir=str(tmp_path),
        dotenv_path=str(tmp_path / "missing.env"),
    )
    settings = bootstrap.initialize()

    for name in (
        "STATE_DIR",
        "HISTORY_DIR",
        "ATTACHMENT_DIR",
        "MERMAID_OUTPUT_DIR",
        "TABLE_RENDERS_DIR",
        "MATH_RENDERS_DIR",
    ):
        assert paths[name].is_dir()
    assert settings.system_prompt == "runtime prompt"
    assert config.SYSTEM_PROMPT == "runtime prompt"
    assert initialize_calls == [False]
    with pytest.raises(FrozenInstanceError):
        settings.state_dir = "changed"  # type: ignore[misc]
    bootstrap._finish_shutdown()


def test_invalid_token_is_rejected_by_settings_bootstrap(tmp_path, monkeypatch):
    _set_runtime_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "   ")

    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        RuntimeBootstrap(
            bot_dir=str(tmp_path),
            dotenv_path=str(tmp_path / "missing.env"),
        ).initialize()


def test_environment_loading_is_idempotent(tmp_path):
    bootstrap = RuntimeBootstrap(
        bot_dir=str(tmp_path),
        dotenv_path=str(tmp_path / ".env"),
    )
    with patch("services.runtime_bootstrap.load_dotenv") as load:
        bootstrap.load_environment()
        bootstrap.load_environment()

    load.assert_called_once_with(str(tmp_path / ".env"), override=False)


def test_importing_entrypoints_does_not_load_dotenv(monkeypatch, tmp_path):
    for module_name in ("main", "main_async"):
        sys.modules.pop(module_name, None)
        with patch("services.runtime_bootstrap.load_dotenv") as load:
            importlib.import_module(module_name)
        load.assert_not_called()

    state = tmp_path / "state"
    env = os.environ.copy()
    env["BOT_STATE_DIR"] = str(state)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(
        [sys.executable, "-c", "import main; import main_async"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
    )
    assert not state.exists()


async def test_async_shutdown_attempts_provider_and_session_cleanup(
    monkeypatch, tmp_path
):
    import services.claude_api as claude_api
    import services.session as session
    from services.codex_app_server import manager

    calls = []

    async def _close_codex():
        calls.append("codex")

    async def _flush_sessions():
        calls.append("sessions")

    monkeypatch.setattr(
        claude_api,
        "kill_orphan_claude_cli",
        lambda: calls.append("claude"),
    )
    monkeypatch.setattr(manager, "close_all_codex_sessions", _close_codex)
    monkeypatch.setattr(session, "flush_session_registry", _flush_sessions)

    bootstrap = _initialized_bootstrap(monkeypatch, tmp_path)
    await bootstrap.shutdown()

    assert calls == ["claude", "codex", "sessions"]


async def test_provider_cleanup_failure_does_not_skip_session_drain(
    monkeypatch, tmp_path
):
    import services.claude_api as claude_api
    import services.session as session
    from services.codex_app_server import manager

    calls = []

    def _kill_claude():
        calls.append("claude")
        raise OSError("claude cleanup failed")

    async def _close_codex():
        calls.append("codex")
        raise OSError("codex cleanup failed")

    async def _flush_sessions():
        calls.append("sessions")

    monkeypatch.setattr(claude_api, "kill_orphan_claude_cli", _kill_claude)
    monkeypatch.setattr(manager, "close_all_codex_sessions", _close_codex)
    monkeypatch.setattr(session, "flush_session_registry", _flush_sessions)

    bootstrap = _initialized_bootstrap(monkeypatch, tmp_path)
    with pytest.raises(RuntimeShutdownError) as exc_info:
        await bootstrap.shutdown()

    assert calls == ["claude", "codex", "sessions"]
    assert [name for name, _ in exc_info.value.failures] == ["claude", "codex"]


async def test_async_shutdown_collects_all_dependencies_before_first_cancel(
    monkeypatch,
    tmp_path,
):
    import services.claude_api as claude_api
    import services.session as session
    from services.codex_app_server import manager

    calls = []
    first_cancel = asyncio.CancelledError("first cleanup cancellation")

    def _cancel_claude():
        calls.append("claude")
        raise first_cancel

    async def _cancel_codex():
        calls.append("codex")
        raise asyncio.CancelledError("second cleanup cancellation")

    async def _flush_sessions():
        calls.append("sessions")

    monkeypatch.setattr(claude_api, "kill_orphan_claude_cli", _cancel_claude)
    monkeypatch.setattr(manager, "close_all_codex_sessions", _cancel_codex)
    monkeypatch.setattr(session, "flush_session_registry", _flush_sessions)
    bootstrap = _initialized_bootstrap(monkeypatch, tmp_path)

    with pytest.raises(asyncio.CancelledError) as caught:
        await bootstrap.shutdown()

    assert caught.value is first_cancel
    assert calls == ["claude", "codex", "sessions"]
    assert bootstrap.state is BootstrapState.CLOSED


def test_sync_shutdown_attempts_provider_and_session_cleanup(monkeypatch, tmp_path):
    import services.claude_api as claude_api
    import services.session as session
    from services.codex_app_server import manager

    calls = []

    async def _close_codex():
        calls.append("codex")

    class Registry:
        def flush_sync(self):
            calls.append("sessions")

    monkeypatch.setattr(
        claude_api,
        "kill_orphan_claude_cli",
        lambda: calls.append("claude"),
    )
    monkeypatch.setattr(manager, "close_all_codex_sessions", _close_codex)
    monkeypatch.setattr(session, "session_registry", Registry)

    bootstrap = _initialized_bootstrap(monkeypatch, tmp_path)
    bootstrap.shutdown_sync()
    bootstrap.shutdown_sync()

    assert calls == ["claude", "codex", "sessions"]


async def test_uninitialized_and_double_shutdown_do_not_close_process_globals(
    monkeypatch,
    tmp_path,
):
    import services.claude_api as claude_api

    calls = []
    monkeypatch.setattr(
        claude_api, "kill_orphan_claude_cli", lambda: calls.append(True)
    )
    fresh = RuntimeBootstrap(bot_dir=str(tmp_path))
    await fresh.shutdown()
    await fresh.shutdown()
    assert calls == []
    assert fresh.state is BootstrapState.CLOSED


def test_partial_initialization_rolls_back_system_prompt_and_has_no_owned_shutdown(
    monkeypatch,
    tmp_path,
):
    import config
    import services.claude_api as claude_api
    import services.session as session

    _set_runtime_env(monkeypatch, tmp_path)
    original_prompt = config.SYSTEM_PROMPT
    monkeypatch.setattr(
        config,
        "load_system_prompt",
        lambda *, bot_dir=None: "temporary runtime prompt",
    )
    monkeypatch.setattr(
        session,
        "initialize_session_registry",
        lambda: (_ for _ in ()).throw(OSError("registry failed")),
    )
    cleanup_calls = []
    monkeypatch.setattr(
        claude_api,
        "kill_orphan_claude_cli",
        lambda: cleanup_calls.append(True),
    )
    bootstrap = RuntimeBootstrap(bot_dir=str(tmp_path))

    with pytest.raises(OSError, match="registry failed"):
        bootstrap.initialize()
    bootstrap.shutdown_sync()

    assert config.SYSTEM_PROMPT == original_prompt
    assert cleanup_calls == []
    assert bootstrap.state is BootstrapState.CLOSED


def test_settings_factory_validates_before_compatibility_commit(monkeypatch, tmp_path):
    import config

    original_port = config.WEB_API_PORT
    environ = dict(os.environ)
    environ.update(
        {
            "DISCORD_TOKEN": "token",
            "BOT_STATE_DIR": str(tmp_path / "state"),
            "BOT_ATTACHMENT_DIR": str(tmp_path / "attachments"),
            "MERMAID_OUTPUT_DIR": str(tmp_path / "mermaid"),
            "TABLE_RENDERS_DIR": str(tmp_path / "table"),
            "MATH_RENDERS_DIR": str(tmp_path / "math"),
            "WEB_API_PORT": "70000",
        }
    )
    with pytest.raises(RuntimeConfigurationError, match="WEB_API_PORT"):
        build_runtime_settings(environ, bot_dir=str(tmp_path))
    assert config.WEB_API_PORT == original_port


def test_symlink_runtime_directory_is_rejected_without_chmodding_target(
    monkeypatch,
    tmp_path,
):
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    state_link = tmp_path / "state"
    state_link.symlink_to(target, target_is_directory=True)
    _set_runtime_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BOT_STATE_DIR", str(state_link))

    with pytest.raises(RuntimeConfigurationError, match="secure runtime directory"):
        RuntimeBootstrap(bot_dir=str(tmp_path)).initialize()

    assert (target.stat().st_mode & 0o777) == 0o755


def test_symlink_state_file_is_rejected(monkeypatch, tmp_path):
    paths = _set_runtime_env(monkeypatch, tmp_path)
    paths["STATE_DIR"].mkdir(mode=0o700)
    victim = tmp_path / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    paths["SESSIONS_PATH"].symlink_to(victim)

    with pytest.raises(RuntimeConfigurationError, match="unsafe type or owner"):
        RuntimeBootstrap(bot_dir=str(tmp_path)).initialize()

    assert victim.read_text(encoding="utf-8") == "{}"


def test_dotenv_values_are_parsed_after_an_early_config_import(monkeypatch, tmp_path):
    import config
    import services.session as session

    paths = _set_runtime_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DISCORD_TOKEN")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DISCORD_TOKEN=from-dotenv\n"
        "WEB_API_PORT=4321\n"
        "MATH_PRESENTATION_MODE=adaptive\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        config,
        "load_system_prompt",
        lambda *, bot_dir=None: "runtime prompt",
    )
    monkeypatch.setattr(session, "SESSIONS_PATH", str(paths["SESSIONS_PATH"]))
    monkeypatch.setattr(session, "initialize_session_registry", lambda: None)

    bootstrap = RuntimeBootstrap(bot_dir=str(tmp_path), dotenv_path=str(dotenv))
    settings = bootstrap.initialize()

    assert settings.discord_token == "from-dotenv"
    assert settings.web_api_port == 4321
    assert config.WEB_API_PORT == 4321
    assert config.MATH_PRESENTATION_GUIDANCE in config.SYSTEM_PROMPT
    assert config.MATH_PRESENTATION_GUIDANCE in config.RESTRICTED_SYSTEM_PROMPT
    bootstrap._finish_shutdown()
    os.environ.pop("DISCORD_TOKEN", None)
    os.environ.pop("WEB_API_PORT", None)
    os.environ.pop("MATH_PRESENTATION_MODE", None)


def test_all_early_imported_path_aliases_install_and_rollback(monkeypatch, tmp_path):
    import config
    import config.paths as config_paths
    import services.session as session

    _set_runtime_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        config,
        "load_system_prompt",
        lambda *, bot_dir=None: "runtime prompt",
    )
    monkeypatch.setattr(session, "initialize_session_registry", lambda: None)
    path_names = (
        "BOT_DIR",
        "STATE_DIR",
        "HISTORY_DIR",
        "SESSIONS_PATH",
        "RUNTIME_STATE_PATH",
        "INVEST_KB_PATH",
        "SETTINGS_PATH",
        "CREDENTIALS_PATH",
        "ATTACHMENT_DIR",
        "MERMAID_OUTPUT_DIR",
        "TABLE_RENDERS_DIR",
        "MATH_RENDERS_DIR",
        "SESSION_TTL_S",
    )
    config_before = {name: getattr(config, name) for name in path_names}
    paths_before = {name: getattr(config_paths, name) for name in path_names}
    bootstrap = RuntimeBootstrap(bot_dir=str(tmp_path))

    settings = bootstrap.initialize()

    for name in path_names:
        assert getattr(config, name) == getattr(config_paths, name)
    assert config.INVEST_KB_PATH == settings.invest_kb_path
    assert config.SETTINGS_PATH == settings.settings_path
    assert config.CREDENTIALS_PATH == settings.credentials_path

    bootstrap._finish_shutdown()
    assert {name: getattr(config, name) for name in path_names} == config_before
    assert {name: getattr(config_paths, name) for name in path_names} == paths_before


async def test_shutdown_requested_during_initialize_never_returns_ready(
    monkeypatch,
    tmp_path,
):
    import config
    import services.claude_api as claude_api
    import services.session as session
    from services.codex_app_server import manager

    _set_runtime_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        config,
        "load_system_prompt",
        lambda *, bot_dir=None: "runtime prompt",
    )
    initialize_started = threading.Event()
    release_initialize = threading.Event()

    def _blocking_initialize_registry():
        initialize_started.set()
        assert release_initialize.wait(timeout=2)

    monkeypatch.setattr(
        session,
        "initialize_session_registry",
        _blocking_initialize_registry,
    )
    monkeypatch.setattr(session, "flush_session_registry", lambda: asyncio.sleep(0))
    monkeypatch.setattr(claude_api, "kill_orphan_claude_cli", lambda: None)
    monkeypatch.setattr(manager, "close_all_codex_sessions", lambda: asyncio.sleep(0))
    bootstrap = RuntimeBootstrap(bot_dir=str(tmp_path))

    initialize_task = asyncio.create_task(asyncio.to_thread(bootstrap.initialize))
    assert await asyncio.to_thread(initialize_started.wait, 2)
    shutdown_task = asyncio.create_task(bootstrap.shutdown())
    while not bootstrap._shutdown_requested:
        await asyncio.sleep(0)
    assert bootstrap.state is BootstrapState.INITIALIZING

    release_initialize.set()
    with pytest.raises(RuntimeError, match="shutdown was requested"):
        await initialize_task
    await shutdown_task

    assert bootstrap.state is BootstrapState.CLOSED


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_child_close_cannot_unlink_or_unlock_parent_pidfile(tmp_path):
    path = tmp_path / "fork-owner.pid"
    lock = PidFileLock(str(path))
    lock.acquire()
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(read_fd)
            lock.close()
            state = "exists" if path.exists() else "missing"
            contender = PidFileLock(str(path))
            try:
                contender.acquire()
            except BlockingIOError:
                state += ":blocked"
            else:
                state += ":acquired"
                contender.close()
            os.write(write_fd, state.encode("ascii"))
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    try:
        child_result = os.read(read_fd, 128).decode("ascii")
        _, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert child_result == "exists:blocked"
        assert path.read_text(encoding="utf-8") == str(os.getpid())
    finally:
        os.close(read_fd)
        lock.close()


def test_root_lifecycle_releases_in_reverse_order_once():
    lifecycle = RootLifecycle()
    calls = []
    lifecycle.callback("first", lambda: calls.append("first"))
    lifecycle.callback("second", lambda: calls.append("second"))

    lifecycle.close()
    lifecycle.close()

    assert calls == ["second", "first"]


def test_root_lifecycle_cancel_still_releases_logging_and_pid_lock(tmp_path):
    lifecycle = RootLifecycle()
    pid_path = tmp_path / "root-lifecycle.pid"
    pid_lock = PidFileLock(str(pid_path))
    pid_lock.acquire()
    lifecycle.callback("pidfile", pid_lock.close)

    events = []

    class Handler(logging.Handler):
        def close(self):
            events.append("logging")
            super().close()

    logger = logging.Logger("root-lifecycle-cancel")
    handler = Handler()
    logger.addHandler(handler)
    lifecycle.own_logging(logger=logger, handler=handler)

    cleanup_cancel = asyncio.CancelledError("callback cancellation")

    def fail() -> None:
        events.append("failure")
        raise OSError("cleanup failed")

    def cancel() -> None:
        events.append("cancel")
        raise cleanup_cancel

    lifecycle.callback("failure", fail)
    lifecycle.callback("cancel", cancel)

    with pytest.raises(asyncio.CancelledError) as caught:
        lifecycle.close()

    assert caught.value is cleanup_cancel
    assert events == ["cancel", "failure", "logging"]
    assert "failure: cleanup failed" in "\n".join(caught.value.__notes__)
    assert handler not in logger.handlers
    assert not pid_path.exists()
    assert lifecycle.closed is True


def test_logging_callback_cancel_still_releases_pid_lock(tmp_path):
    lifecycle = RootLifecycle()
    pid_path = tmp_path / "logging-cancel.pid"
    pid_lock = PidFileLock(str(pid_path))
    pid_lock.acquire()
    lifecycle.callback("pidfile", pid_lock.close)
    cleanup_cancel = asyncio.CancelledError("handler close cancelled")
    events = []

    class CancellingHandler(logging.Handler):
        cancel_once = True

        def close(self):
            events.append("handler")
            if self.cancel_once:
                self.cancel_once = False
                raise cleanup_cancel
            super().close()

    logger = logging.Logger("logging-cancel")
    handler = CancellingHandler()
    logger.addHandler(handler)
    lifecycle.own_logging(logger=logger, handler=handler)

    with pytest.raises(asyncio.CancelledError) as caught:
        lifecycle.close()

    assert caught.value is cleanup_cancel
    assert events == ["handler"]
    assert handler not in logger.handlers
    assert not pid_path.exists()
    assert lifecycle.closed is True


async def test_root_lifecycle_aclose_survives_repeated_outer_cancel(monkeypatch):
    lifecycle = RootLifecycle()
    events = []
    lifecycle.callback("first", lambda: events.append("first"))
    callback_cancel = asyncio.CancelledError("callback cancellation")
    fatal = KeyboardInterrupt("callback fatal")

    def fail() -> None:
        events.append("failure")
        raise OSError("callback failure")

    def fail_fatally() -> None:
        events.append("fatal")
        raise fatal

    def cancel() -> None:
        events.append("callback-cancel")
        raise callback_cancel

    lifecycle.callback("failure", fail)
    lifecycle.callback("fatal", fail_fatally)
    lifecycle.callback("cancel", cancel)
    worker_started = asyncio.Event()
    release_worker = asyncio.Event()
    original_worker = lifecycle._run_close_task

    async def delayed_worker():
        worker_started.set()
        await release_worker.wait()
        return await original_worker()

    monkeypatch.setattr(lifecycle, "_run_close_task", delayed_worker)
    closing = asyncio.create_task(lifecycle.aclose())
    await worker_started.wait()

    closing.cancel("first cancellation")
    await asyncio.sleep(0)
    closing.cancel("second cancellation")
    await asyncio.sleep(0)
    release_worker.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await closing

    assert caught.value.args == ("first cancellation",)
    assert caught.value is not callback_cancel
    assert caught.value.__context__ is fatal
    diagnostics = "\n".join(caught.value.__notes__)
    assert "callback cancellation" in diagnostics
    assert "callback fatal" in diagnostics
    assert "callback failure" in diagnostics
    assert events == ["callback-cancel", "fatal", "failure", "first"]
    assert lifecycle.closed is True
    assert lifecycle._close_task is not None
    assert lifecycle._close_task.done()
    assert not any(
        task.get_name() == "root-lifecycle-cleanup" and not task.done()
        for task in asyncio.all_tasks()
    )


def test_root_lifecycle_baseexception_wins_without_cancellation():
    lifecycle = RootLifecycle()
    events = []
    fatal = SystemExit("fatal cleanup")

    def fail() -> None:
        events.append("failure")
        raise OSError("ordinary cleanup failure")

    def fail_fatally() -> None:
        events.append("fatal")
        raise fatal

    lifecycle.callback("after", lambda: events.append("after"))
    lifecycle.callback("failure", fail)
    lifecycle.callback("fatal", fail_fatally)

    with pytest.raises(SystemExit) as caught:
        lifecycle.close()

    assert caught.value is fatal
    assert events == ["fatal", "failure", "after"]
    assert "ordinary cleanup failure" in "\n".join(caught.value.__notes__)


def test_root_lifecycle_fatal_wins_over_callback_cancellation():
    lifecycle = RootLifecycle()
    callback_cancel = asyncio.CancelledError("callback cancellation")
    fatal = KeyboardInterrupt("fatal cleanup")
    events = []

    def cancel() -> None:
        events.append("cancel")
        raise callback_cancel

    def fail_fatally() -> None:
        events.append("fatal")
        raise fatal

    lifecycle.callback("ordinary", lambda: events.append("ordinary"))
    lifecycle.callback("fatal", fail_fatally)
    lifecycle.callback("cancel", cancel)

    with pytest.raises(KeyboardInterrupt) as caught:
        lifecycle.close()

    assert caught.value is fatal
    assert events == ["cancel", "fatal", "ordinary"]
    assert "callback cancellation" in "\n".join(caught.value.__notes__)


def test_root_lifecycle_unregisters_atexit(monkeypatch):
    registered = []
    unregistered = []
    monkeypatch.setattr("atexit.register", lambda callback: registered.append(callback))
    monkeypatch.setattr(
        "atexit.unregister", lambda callback: unregistered.append(callback)
    )
    lifecycle = RootLifecycle()

    lifecycle.arm_atexit()
    lifecycle.close()

    assert registered == [lifecycle.close]
    assert unregistered == [lifecycle.close]


def test_root_lifecycle_restores_original_signal_handler(monkeypatch):
    calls = []
    original_handler = lambda *_args: None

    class Loop:
        def add_signal_handler(self, sig, callback, *args):
            calls.append(("add", sig, callback, args))

        def remove_signal_handler(self, sig):
            calls.append(("remove", sig))
            return True

    restored = []
    monkeypatch.setattr(signal, "getsignal", lambda sig: original_handler)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda sig, handler: restored.append((sig, handler)),
    )
    lifecycle = RootLifecycle()

    lifecycle.add_signal_handler(Loop(), signal.SIGTERM, lambda: None)
    lifecycle.close()

    assert ("remove", signal.SIGTERM) in calls
    assert restored == [(signal.SIGTERM, original_handler)]


def test_signal_registration_rolls_back_if_lifecycle_closes_reentrantly(
    monkeypatch,
):
    lifecycle = RootLifecycle()
    calls = []
    original_handler = lambda *_args: None

    class Loop:
        def add_signal_handler(self, sig, callback, *args):
            calls.append(("add", sig))
            lifecycle.close()

        def remove_signal_handler(self, sig):
            calls.append(("remove", sig))
            return True

    monkeypatch.setattr(signal, "getsignal", lambda _sig: original_handler)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda sig, handler: calls.append(("restore", sig, handler)),
    )

    installed = lifecycle.add_signal_handler(
        Loop(),
        signal.SIGTERM,
        lambda: None,
    )

    assert installed is False
    assert calls == [
        ("add", signal.SIGTERM),
        ("remove", signal.SIGTERM),
        ("restore", signal.SIGTERM, original_handler),
    ]


def test_root_lifecycle_removes_then_closes_owned_handler():
    events = []

    class Handler(logging.Handler):
        def close(self):
            events.append("close")
            super().close()

    class Logger(logging.Logger):
        def removeHandler(self, handler):
            events.append("remove")
            super().removeHandler(handler)

    logger = Logger("owned")
    handler = Handler()
    logger.addHandler(handler)
    lifecycle = RootLifecycle()
    lifecycle.own_logging(logger=logger, handler=handler)

    lifecycle.close()
    lifecycle.close()

    assert events == ["remove", "close"]

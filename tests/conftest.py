"""Shared fixtures for tests."""

import json
import os
import tempfile

import pytest

# Redirect any file logging set up during tests away from the production
# bot.log. Some tests exercise main/main_async._setup_logging(); without this
# guard their RotatingFileHandler targets the live log and pytest tracebacks
# get interleaved with real bot lines, poisoning the error-rate monitor
# (observed 2026-07-03). Set at import time so it precedes ALL collection.
os.environ.setdefault(
    "BOT_LOG_PATH", os.path.join(tempfile.gettempdir(), "discord-bot-test.log")
)


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    """Provide a temporary state directory and patch config paths."""
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    sessions_path = tmp_path / "sessions.json"

    monkeypatch.setattr("config.STATE_DIR", str(tmp_path))
    monkeypatch.setattr("config.HISTORY_DIR", str(history_dir))
    monkeypatch.setattr("config.SESSIONS_PATH", str(sessions_path))

    # Also patch the session module's imported references
    monkeypatch.setattr("services.session.HISTORY_DIR", str(history_dir))
    monkeypatch.setattr("services.session.SESSIONS_PATH", str(sessions_path))

    return tmp_path


@pytest.fixture
def tmp_attachment_dir(tmp_path, monkeypatch):
    """Provide a temporary attachment directory."""
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    monkeypatch.setattr("config.ATTACHMENT_DIR", str(att_dir))
    monkeypatch.setattr("utils.attachments.ATTACHMENT_DIR", str(att_dir))
    return att_dir


@pytest.fixture(autouse=True)
def _isolate_bot_state():
    """Reset bot.py module-level mutable state between every test.

    `bot.cooldowns` and `bot._daily_quota` are OrderedDicts mutated by the
    on_message hot path. Without isolation, test order influences outcomes
    and pytest-xdist (introduced in this PR) makes the order non-deterministic
    within a worker. Importing `bot` is best-effort: pure-utility tests that
    do not import bot will skip the reset silently.
    """
    try:
        import bot as _bot
    except Exception:
        yield
        return
    cooldowns = getattr(_bot, "cooldowns", None)
    daily_quota = getattr(_bot, "_daily_quota", None)
    if cooldowns is not None:
        cooldowns.clear()
    if daily_quota is not None:
        daily_quota.clear()
    # P4a: bot.sessions is now the process-wide shared dict from
    # services.session.shared_sessions(). Clear its CONTENTS (not rebind —
    # bot.py and web_api hold references to the same object) so entries
    # created by one test never leak into the next.
    shared = getattr(_bot, "sessions", None)
    if shared is not None:
        shared.clear()
    yield
    if cooldowns is not None:
        cooldowns.clear()
    if daily_quota is not None:
        daily_quota.clear()

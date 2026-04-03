"""Shared fixtures for tests."""

import json
import os
import tempfile

import pytest


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

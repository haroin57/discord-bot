from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from config.paths import PathConfigurationError, build_runtime_paths


def test_instance_paths_are_isolated_without_import_time_io(monkeypatch, tmp_path):
    state_dir = tmp_path / "hermes-state"
    attachment_dir = tmp_path / "hermes-attachments"
    monkeypatch.setenv("BOT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("BOT_ATTACHMENT_DIR", str(attachment_dir))

    source = Path(__file__).parents[1] / "config" / "paths.py"
    spec = importlib.util.spec_from_file_location("isolated_config_paths", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.STATE_DIR == str(state_dir)
    assert module.HISTORY_DIR == str(state_dir / "history")
    assert module.SESSIONS_PATH == str(state_dir / "sessions.json")
    assert module.RUNTIME_STATE_PATH == str(state_dir / "runtime_state.json")
    assert module.ATTACHMENT_DIR == str(attachment_dir)
    assert not state_dir.exists()
    assert not (state_dir / "history").exists()
    assert not attachment_dir.exists()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BOT_STATE_DIR", ""),
        ("BOT_STATE_DIR", "relative/state"),
        ("BOT_STATE_DIR", "/etc/discord-bot-state"),
        ("BOT_ATTACHMENT_DIR", "/home/unapproved/attachments"),
    ],
)
def test_runtime_path_factory_rejects_empty_relative_and_unapproved_paths(
    tmp_path,
    name,
    value,
):
    environ = dict(os.environ)
    environ.update(
        {
            "BOT_STATE_DIR": str(tmp_path / "state"),
            "BOT_ATTACHMENT_DIR": str(tmp_path / "attachments"),
            "MERMAID_OUTPUT_DIR": str(tmp_path / "mermaid"),
            "TABLE_RENDERS_DIR": str(tmp_path / "table"),
            "MATH_RENDERS_DIR": str(tmp_path / "math"),
            name: value,
        }
    )
    with pytest.raises(PathConfigurationError, match=name):
        build_runtime_paths(environ, bot_dir=str(tmp_path))

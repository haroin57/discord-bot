from __future__ import annotations

import importlib.util
from pathlib import Path


def test_instance_paths_can_be_isolated(monkeypatch, tmp_path):
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
    assert state_dir.is_dir()
    assert (state_dir / "history").is_dir()
    assert attachment_dir.is_dir()

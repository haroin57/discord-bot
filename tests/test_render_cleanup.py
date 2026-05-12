import os
import time
from unittest.mock import patch
import pytest


def test_cleanup_walks_all_render_dirs(tmp_path, monkeypatch):
    """Files older than 1h in any of the 4 directories must be removed."""
    from utils import attachments

    dirs = {}
    for name in ("attach", "mermaid", "table", "math"):
        d = tmp_path / name
        d.mkdir()
        dirs[name] = d
        # Create a stale file (mtime = 2 hours ago)
        stale = d / f"{name}.png"
        stale.write_bytes(b"stale")
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        # Create a fresh file
        fresh = d / f"{name}_recent.png"
        fresh.write_bytes(b"fresh")

    monkeypatch.setattr(attachments, "ATTACHMENT_DIR", str(dirs["attach"]))
    monkeypatch.setattr("config.ATTACHMENT_DIR", str(dirs["attach"]))
    monkeypatch.setattr("config.MERMAID_OUTPUT_DIR", str(dirs["mermaid"]))
    monkeypatch.setattr("config.TABLE_RENDERS_DIR", str(dirs["table"]))
    monkeypatch.setattr("config.MATH_RENDERS_DIR", str(dirs["math"]))

    attachments.cleanup_stale_attachments()

    for name, d in dirs.items():
        assert not (d / f"{name}.png").exists(), f"stale {name} not removed"
        assert (d / f"{name}_recent.png").exists(), f"fresh {name} wrongly removed"


def test_cleanup_handles_missing_directory(tmp_path, monkeypatch):
    """Non-existent render dir should not raise."""
    from utils import attachments
    monkeypatch.setattr("config.MERMAID_OUTPUT_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setattr("config.TABLE_RENDERS_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setattr("config.MATH_RENDERS_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setattr("config.ATTACHMENT_DIR", str(tmp_path / "nonexistent"))
    # Should not raise
    attachments.cleanup_stale_attachments()

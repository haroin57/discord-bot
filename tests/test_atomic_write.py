"""Tests for utils.atomic_write — JSON / text / bytes atomic writers."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from utils.atomic_write import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_atomic_write_bytes_basic(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    payload = b"\x00\x01\x02hello\xff"
    atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def test_atomic_write_bytes_mode_default_is_600(tmp_path: Path) -> None:
    target = tmp_path / "secret.bin"
    atomic_write_bytes(target, b"abc")
    assert _mode(target) == 0o600


def test_atomic_write_bytes_custom_mode(tmp_path: Path) -> None:
    target = tmp_path / "world.bin"
    atomic_write_bytes(target, b"abc", mode=0o644)
    assert _mode(target) == 0o644


def test_atomic_write_text_basic(tmp_path: Path) -> None:
    target = tmp_path / "log.txt"
    atomic_write_text(target, "日本語 ok\n")
    assert target.read_text(encoding="utf-8") == "日本語 ok\n"


def test_atomic_write_text_custom_encoding(tmp_path: Path) -> None:
    target = tmp_path / "shift.txt"
    atomic_write_text(target, "あ", encoding="utf-16")
    assert target.read_text(encoding="utf-16") == "あ"


def test_atomic_write_json_basic(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    data = {"k": "v", "list": [1, 2, 3], "nested": {"a": "あ"}}
    atomic_write_json(target, data)
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == data


def test_atomic_write_json_no_indent(tmp_path: Path) -> None:
    target = tmp_path / "compact.json"
    atomic_write_json(target, {"k": "v"}, indent=None)
    assert "\n" not in target.read_text(encoding="utf-8")


def test_atomic_write_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "out.json"
    atomic_write_json(target, {"x": 1})
    assert target.is_file()


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_no_tmp_leftover_on_success(tmp_path: Path) -> None:
    target = tmp_path / "ok.json"
    atomic_write_json(target, {"x": 1})
    leftover = list(tmp_path.glob("ok.json.*.tmp"))
    assert leftover == []


def test_atomic_write_cleans_tmp_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If os.replace fails, the tmp file should be removed."""
    target = tmp_path / "boom.txt"

    def _bad_replace(src, dst):
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", _bad_replace)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_text(target, "hello")
    assert list(tmp_path.glob("boom.txt.*.tmp")) == []

"""Unit tests for services.vscode_sync.tailer."""

import json
from pathlib import Path

from services.vscode_sync.tailer import JSONLTailer


def _write_jsonl(path: Path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _append_jsonl(path: Path, events):
    with open(path, "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_prime_skips_existing_content(tmp_path: Path):
    f = tmp_path / "proj-a" / "session1.jsonl"
    _write_jsonl(f, [{"a": 1}, {"a": 2}])

    t = JSONLTailer(tmp_path)
    t.prime()
    assert t.poll() == []


def test_new_content_after_prime(tmp_path: Path):
    f = tmp_path / "proj" / "s.jsonl"
    _write_jsonl(f, [{"old": True}])

    t = JSONLTailer(tmp_path)
    t.prime()

    _append_jsonl(f, [{"new": True}])
    out = t.poll()
    assert len(out) == 1
    assert out[0].raw == {"new": True}
    assert out[0].path == f


def test_new_file_after_prime_starts_from_zero(tmp_path: Path):
    t = JSONLTailer(tmp_path)
    t.prime()

    f = tmp_path / "proj" / "s.jsonl"
    _write_jsonl(f, [{"first": 1}, {"second": 2}])

    out = t.poll()
    assert len(out) == 2
    assert out[0].raw == {"first": 1}
    assert out[1].raw == {"second": 2}


def test_partial_trailing_line_held_back(tmp_path: Path):
    sub = tmp_path / "proj"
    sub.mkdir()
    f = sub / "s.jsonl"
    t = JSONLTailer(tmp_path)
    t.prime()

    f.write_bytes(b'{"a": 1}\n{"b": ')
    out = t.poll()
    assert len(out) == 1
    assert out[0].raw == {"a": 1}

    with open(f, "ab") as fh:
        fh.write(b'2}\n')
    out = t.poll()
    assert len(out) == 1
    assert out[0].raw == {"b": 2}


def test_bad_json_skipped(tmp_path: Path):
    sub = tmp_path / "proj"
    f = sub / "s.jsonl"
    sub.mkdir()
    f.write_text('{"good": 1}\nnot json\n{"good": 2}\n', encoding="utf-8")

    t = JSONLTailer(tmp_path)
    out = t.poll()
    raws = [tl.raw for tl in out]
    assert {"good": 1} in raws
    assert {"good": 2} in raws
    assert len(out) == 2


def test_inode_rotation_resets_offset(tmp_path: Path):
    f = tmp_path / "proj" / "s.jsonl"
    _write_jsonl(f, [{"old1": 1}, {"old2": 2}])

    t = JSONLTailer(tmp_path)
    t.prime()

    f.unlink()
    _write_jsonl(f, [{"new1": 1}])

    out = t.poll()
    assert {"new1": 1} in [tl.raw for tl in out]


def test_truncated_file_reread(tmp_path: Path):
    f = tmp_path / "proj" / "s.jsonl"
    _write_jsonl(f, [{"x": 1}, {"x": 2}, {"x": 3}])

    t = JSONLTailer(tmp_path)
    t.prime()

    f.write_text('{"truncated": true}\n', encoding="utf-8")
    out = t.poll()
    assert {"truncated": True} in [tl.raw for tl in out]


def test_empty_file_no_events(tmp_path: Path):
    f = tmp_path / "proj" / "s.jsonl"
    f.parent.mkdir()
    f.write_bytes(b"")

    t = JSONLTailer(tmp_path)
    t.prime()
    assert t.poll() == []


def test_multiple_projects(tmp_path: Path):
    a = tmp_path / "proj-a" / "s1.jsonl"
    b = tmp_path / "proj-b" / "s2.jsonl"
    _write_jsonl(a, [{"a": 1}])
    _write_jsonl(b, [{"b": 1}])

    t = JSONLTailer(tmp_path)
    out = t.poll()
    raws = [tl.raw for tl in out]
    assert {"a": 1} in raws
    assert {"b": 1} in raws


def test_missing_root_does_not_raise(tmp_path: Path):
    t = JSONLTailer(tmp_path / "does-not-exist")
    t.prime()
    assert t.poll() == []

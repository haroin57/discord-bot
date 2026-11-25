"""Bound JSON decoding on history replay without changing row semantics."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from services import session


def test_tail_decodes_only_the_requested_recent_rows(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    rows = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
        for index in range(100)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    loads = json.loads
    decoded = []

    def counted_loads(value):
        decoded.append(value)
        return loads(value)

    monkeypatch.setattr(session.json, "loads", counted_loads)
    assert session._tail_jsonl(str(path), 4) == rows[-4:]
    assert len(decoded) == 4


def test_tail_skips_invalid_nonconversation_and_expired_rows(tmp_path):
    path = tmp_path / "history.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    rows = [
        {"role": "user", "content": "oldest kept"},
        {"role": "assistant", "content": "reply"},
        {"role": "channel", "content": "unaddressed"},
        {"role": "user", "content": "expired", "timestamp": old},
        {"role": "user", "content": "latest"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\ninvalid-json\n")
    result = session._tail_jsonl(str(path), 3, max_age_days=1)
    assert [row["content"] for row in result] == ["oldest kept", "reply", "latest"]


def test_tail_preserves_structured_replay_snapshot_and_summary(tmp_path):
    path = tmp_path / "history.jsonl"
    transcript = [{"role": "assistant", "content": [{"type": "text", "text": "reply"}]}]
    rows = [
        {"role": "user", "content": "discard"},
        {
            "role": "user", "content": "要約", "is_compact_summary": True,
            "keep_tail_rows": 2, "author": "speaker", "context_snapshot": "memory",
            "timestamp": "2026-09-16T00:00:00+00:00",
        },
        {"role": "assistant", "content": "返答", "api_messages": transcript},
    ]
    # Include a multibyte value and no final newline, as interrupted writers can.
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows))
    assert session._tail_jsonl(str(path), 2, include_context_snapshot=True) == rows[-2:]


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_tail_limit_does_not_decode(tmp_path, monkeypatch, limit):
    path = tmp_path / "history.jsonl"
    path.write_text('{"role":"user","content":"unneeded"}\n')

    def unexpected(*args, **kwargs):
        pytest.fail("An empty window must not parse history")

    monkeypatch.setattr(session.json, "loads", unexpected)
    assert session._tail_jsonl(str(path), limit) == []

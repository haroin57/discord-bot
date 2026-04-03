"""Tests for services/session.py — persistence and history integrity."""

import json
from collections import OrderedDict

from services.session import (
    append_jsonl,
    fix_history_integrity,
    load_all_history,
    load_history_channel,
    load_sessions,
    save_sessions,
    trim_history,
    update_session,
)


class TestSessions:
    def test_save_and_load(self, tmp_state_dir):
        s = OrderedDict([(1, "sess-a"), (2, "sess-b")])
        save_sessions(s)
        loaded = load_sessions()
        assert loaded == s

    def test_load_missing_file(self, tmp_state_dir):
        assert load_sessions() == OrderedDict()

    def test_update_session_lru(self, tmp_state_dir):
        s = OrderedDict()
        for i in range(105):
            update_session(s, i, f"sess-{i}")
        assert len(s) == 100
        assert 0 not in s  # oldest evicted
        assert 104 in s


class TestJSONLHistory:
    def test_append_and_load(self, tmp_state_dir):
        append_jsonl(42, {"role": "user", "content": "hello"})
        append_jsonl(42, {"role": "assistant", "content": "hi"})
        msgs = load_history_channel(42)
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "hi"}

    def test_load_missing_channel(self, tmp_state_dir):
        assert load_history_channel(999) == []

    def test_load_all_history(self, tmp_state_dir):
        append_jsonl(1, {"role": "user", "content": "a"})
        append_jsonl(1, {"role": "assistant", "content": "b"})
        append_jsonl(2, {"role": "user", "content": "c"})
        append_jsonl(2, {"role": "assistant", "content": "d"})
        result = load_all_history()
        assert len(result) == 2
        assert 1 in result
        assert 2 in result

    def test_ignores_non_message_entries(self, tmp_state_dir):
        append_jsonl(10, {"role": "user", "content": "msg1"})
        append_jsonl(10, {"type": "system", "data": "noise"})  # not user/assistant
        append_jsonl(10, {"role": "assistant", "content": "msg2"})
        msgs = load_history_channel(10)
        assert len(msgs) == 2


class TestTrimHistory:
    def test_no_trim_when_under_limit(self):
        h = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        trim_history(h, max_pairs=5)
        assert len(h) == 2

    def test_trims_to_limit(self):
        h = []
        for i in range(30):
            h.append({"role": "user", "content": f"u{i}"})
            h.append({"role": "assistant", "content": f"a{i}"})
        trim_history(h, max_pairs=5)
        assert len(h) <= 10
        assert h[0]["role"] == "user"

    def test_preserves_pair_alignment(self):
        h = [
            {"role": "assistant", "content": "orphan"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        trim_history(h, max_pairs=1)
        assert h[0]["role"] == "user"


class TestFixHistoryIntegrity:
    def test_removes_dangling_user(self):
        h = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "dangling"},
        ]
        fix_history_integrity(h)
        assert len(h) == 2
        assert h[-1]["role"] == "assistant"

    def test_removes_leading_assistant(self):
        h = [
            {"role": "assistant", "content": "orphan"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        fix_history_integrity(h)
        assert h[0]["role"] == "user"

    def test_empty_list(self):
        h: list[dict] = []
        fix_history_integrity(h)
        assert h == []

    def test_valid_history_unchanged(self):
        h = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        fix_history_integrity(h)
        assert len(h) == 2

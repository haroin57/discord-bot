"""Tests for services/session.py — persistence and history integrity."""

import json
from collections import OrderedDict

import pytest

from services.session import (
    SessionInfo,
    _history_file,
    append_jsonl,
    fix_history_integrity,
    get_session,
    load_history_channel,
    load_sessions,
    save_sessions,
    set_session,
    trim_history,
    update_session,
)
import services.session as _session_module


@pytest.fixture(autouse=True)
def _clear_sessions_store():
    """Isolate the module-level _sessions_store between tests."""
    _session_module._sessions_store.clear()
    yield
    _session_module._sessions_store.clear()


class TestCompositeKey:
    def test_composite_key_isolates_users(self, tmp_state_dir):
        info_a = SessionInfo(session_id="A", mcp_server_keys=[])
        info_b = SessionInfo(session_id="B", mcp_server_keys=[])
        set_session(100, 1, info_a)
        set_session(100, 2, info_b)
        assert get_session(100, 1).session_id == "A"
        assert get_session(100, 2).session_id == "B"

    def test_legacy_no_user_id_bucket(self, tmp_state_dir):
        info = SessionInfo(session_id="X", mcp_server_keys=[])
        set_session(100, None, info)
        assert get_session(100, None).session_id == "X"
        assert get_session(100, 999) is None  # not visible to other users

    def test_history_file_rejects_traversal(self):
        with pytest.raises(ValueError):
            _history_file(-1)
        with pytest.raises(ValueError):
            _history_file(0)
        with pytest.raises(ValueError):
            _history_file(1)  # too small


class TestSessions:
    def test_save_and_load(self, tmp_state_dir):
        s: OrderedDict[int, SessionInfo] = OrderedDict([
            (1, SessionInfo(session_id="sess-a", mcp_server_keys=["context7"])),
            (2, SessionInfo(session_id="sess-b", mcp_server_keys=["brave-search"])),
        ])
        save_sessions(s)
        loaded = load_sessions()
        assert loaded[1].session_id == "sess-a"
        assert loaded[1].mcp_server_keys == ["context7"]
        assert loaded[2].session_id == "sess-b"

    def test_load_missing_file(self, tmp_state_dir):
        assert load_sessions() == OrderedDict()

    def test_load_backward_compat(self, tmp_state_dir):
        """Old format (plain string values) should still load."""
        import config
        with open(config.SESSIONS_PATH, "w") as f:
            json.dump({"1": "old-sess-id", "2": "old-sess-id-2"}, f)
        loaded = load_sessions()
        assert loaded[1].session_id == "old-sess-id"
        assert loaded[1].mcp_server_keys == []
        assert loaded[2].session_id == "old-sess-id-2"

    def test_update_session_lru(self, tmp_state_dir):
        s: OrderedDict[int, SessionInfo] = OrderedDict()
        for i in range(105):
            update_session(s, i, SessionInfo(session_id=f"sess-{i}"))
        assert len(s) == 100
        assert 0 not in s  # oldest evicted
        assert 104 in s

    def test_update_session_stores_mcp_keys(self, tmp_state_dir):
        s: OrderedDict[int, SessionInfo] = OrderedDict()
        info = SessionInfo(session_id="s1", mcp_server_keys=["context7", "brave-search"])
        update_session(s, 42, info)
        assert s[42].mcp_server_keys == ["context7", "brave-search"]
        # Verify it persists to disk
        loaded = load_sessions()
        assert loaded[42].mcp_server_keys == ["context7", "brave-search"]


# Minimal valid Discord snowflake IDs (must be in [10**14, 10**19])
_CH_A = 100000000000000   # 10**14
_CH_B = 100000000000001
_CH_MISSING = 999999999999999


class TestJSONLHistory:
    def test_append_and_load(self, tmp_state_dir):
        append_jsonl(_CH_A, {"role": "user", "content": "hello"})
        append_jsonl(_CH_A, {"role": "assistant", "content": "hi"})
        msgs = load_history_channel(_CH_A)
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "hi"}

    def test_load_missing_channel(self, tmp_state_dir):
        assert load_history_channel(_CH_MISSING) == []

    def test_ignores_non_message_entries(self, tmp_state_dir):
        append_jsonl(_CH_B, {"role": "user", "content": "msg1"})
        append_jsonl(_CH_B, {"type": "system", "data": "noise"})  # not user/assistant
        append_jsonl(_CH_B, {"role": "assistant", "content": "msg2"})
        msgs = load_history_channel(_CH_B)
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

"""Tests for services/claude_api.py — normalize_messages."""

from services.claude_api import normalize_messages


class TestNormalizeMessages:
    def test_empty(self):
        assert normalize_messages([]) == []

    def test_no_merge_needed(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        assert normalize_messages(msgs) == msgs

    def test_merges_consecutive_user_strings(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 1
        assert "first" in result[0]["content"]
        assert "second" in result[0]["content"]

    def test_merges_consecutive_user_lists_with_hoist(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "result1"},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "world"},
            ]},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 1
        content = result[0]["content"]
        # tool_result should be hoisted to front
        assert content[0]["type"] == "tool_result"
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert len(text_blocks) == 2

    def test_does_not_merge_different_roles(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 3

    def test_does_not_merge_mixed_types(self):
        msgs = [
            {"role": "user", "content": "string"},
            {"role": "user", "content": [{"type": "text", "text": "list"}]},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 2  # can't merge str + list

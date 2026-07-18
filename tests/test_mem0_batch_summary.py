"""TDD Phase 2: preserve more per-turn detail before extraction.

The batch summary used to truncate each turn to 300 chars (bot.py) and the
extractor then capped its whole input at 2000 chars — so a 10-turn batch
(~3500-4000 chars in prod) lost ~40% before the LLM ever saw it, on top of the
per-turn cut. These tests pin a configurable, testable summary builder.
"""

import pytest


@pytest.fixture
def build_batch_summary():
    from services.mem0.batch import build_batch_summary as builder

    return builder


class TestBuildBatchSummary:
    def test_format_is_user_assistant_lines(self, build_batch_summary):
        turns = [{"user": "好きな酒は?", "assistant": "ウイスキーです"}]
        out = build_batch_summary(turns)
        assert "user: 好きな酒は?" in out
        assert "assistant: ウイスキーです" in out

    def test_respects_turn_char_cap(self, build_batch_summary):
        turns = [{"user": "あ" * 2000, "assistant": "い" * 2000}]
        out = build_batch_summary(turns, turn_chars=600)
        assert out.count("あ") == 600
        assert out.count("い") == 600

    def test_default_cap_larger_than_old_300(self, build_batch_summary):
        # Regression guard: the old hard 300 gutted long technical turns.
        turns = [{"user": "x" * 800, "assistant": ""}]
        out = build_batch_summary(turns)
        assert out.count("x") > 300

    def test_multiple_turns_joined(self, build_batch_summary):
        turns = [
            {"user": "u1", "assistant": "a1"},
            {"user": "u2", "assistant": "a2"},
        ]
        out = build_batch_summary(turns)
        assert "u1" in out and "u2" in out and "a1" in out and "a2" in out

    def test_missing_keys_safe(self, build_batch_summary):
        out = build_batch_summary([{"user": "only-user"}])
        assert "only-user" in out

    def test_empty_turns(self, build_batch_summary):
        assert build_batch_summary([]) == ""

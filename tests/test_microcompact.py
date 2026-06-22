"""TDD: time-based microcompact, ported from Claude Code CLI's wF_/$F_.

On a conversation time-gap the real CLI reclaims tokens cheaply (no LLM call)
by replacing the *content* of OLD tool_result blocks with a placeholder while
keeping the last `keep_recent` tool outputs verbatim. Message structure and
ordering are never touched — only bulky tool bodies shrink.
"""

from services.claude_api.microcompact import (
    PLACEHOLDER,
    microcompact_tool_results,
)


def _assistant_tool_use(tid: str):
    return {"role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {}}]}


def _user_tool_result(tid: str, body: str):
    return {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": body}]}


def _convo(n: int):
    """n turns of (assistant tool_use tid_i) + (user tool_result tid_i big body)."""
    msgs = []
    for i in range(n):
        msgs.append(_assistant_tool_use(f"t{i}"))
        msgs.append(_user_tool_result(f"t{i}", f"RESULT-{i}-" + "x" * 500))
    return msgs


class TestMicrocompact:
    def test_no_op_when_gap_below_threshold(self):
        msgs = _convo(10)
        out, saved = microcompact_tool_results(
            msgs, gap_minutes=5, gap_threshold_min=30, keep_recent=3)
        assert saved == 0
        assert out == msgs  # untouched

    def test_clears_old_keeps_recent(self):
        msgs = _convo(10)
        out, saved = microcompact_tool_results(
            msgs, gap_minutes=45, gap_threshold_min=30, keep_recent=3)
        assert saved > 0
        # last 3 tool_results (t7,t8,t9) keep their bodies
        kept_bodies = []
        cleared = 0
        for m in out:
            for b in (m["content"] if isinstance(m["content"], list) else []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    if b["content"] == PLACEHOLDER:
                        cleared += 1
                    else:
                        kept_bodies.append(b["tool_use_id"])
        assert cleared == 7          # t0..t6 cleared
        assert set(kept_bodies) == {"t7", "t8", "t9"}

    def test_structure_and_count_preserved(self):
        msgs = _convo(6)
        out, _ = microcompact_tool_results(
            msgs, gap_minutes=60, gap_threshold_min=30, keep_recent=2)
        assert len(out) == len(msgs)
        assert [m["role"] for m in out] == [m["role"] for m in msgs]

    def test_noop_when_at_or_below_keep_recent(self):
        msgs = _convo(3)
        out, saved = microcompact_tool_results(
            msgs, gap_minutes=120, gap_threshold_min=30, keep_recent=3)
        assert saved == 0
        assert out == msgs

    def test_already_cleared_not_recounted(self):
        msgs = _convo(8)
        out1, saved1 = microcompact_tool_results(
            msgs, gap_minutes=45, gap_threshold_min=30, keep_recent=2)
        assert saved1 > 0
        out2, saved2 = microcompact_tool_results(
            out1, gap_minutes=45, gap_threshold_min=30, keep_recent=2)
        assert saved2 == 0  # nothing new to clear

    def test_non_tool_messages_untouched(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ] + _convo(6)
        out, _ = microcompact_tool_results(
            msgs, gap_minutes=45, gap_threshold_min=30, keep_recent=2)
        assert out[0] == {"role": "user", "content": "hello"}
        assert out[1] == {"role": "assistant", "content": "hi"}

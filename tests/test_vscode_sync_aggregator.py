"""Unit tests for services.vscode_sync.aggregator."""

import asyncio
from datetime import datetime, timezone

import pytest

from services.vscode_sync.aggregator import EventAggregator, _split_chunks
from services.vscode_sync.parser import SyncEvent


def _ev(kind, sid="s1", **payload):
    return SyncEvent(
        kind=kind,
        session_id=sid,
        cwd="/cwd",
        git_branch="master",
        timestamp=datetime.now(timezone.utc),
        payload=payload,
    )


async def _collect(events, flush_interval=0.05):
    posted: list[str] = []

    async def cb(content: str):
        posted.append(content)

    agg = EventAggregator(flush_callback=cb, flush_interval=flush_interval)
    for ev in events:
        agg.push(ev)
    await asyncio.sleep(flush_interval + 0.1)
    return posted, agg


async def test_user_prompt_single_post():
    posted, _ = await _collect([_ev("user_prompt", text="hello world")])
    assert len(posted) == 1
    assert "hello world" in posted[0]
    assert "👤" in posted[0]


async def test_session_start_header_on_first_event():
    posted, _ = await _collect([_ev("user_prompt", text="first prompt", sid="abcdef0123456789")])
    assert "abcdef01" in posted[0]
    assert "/cwd" in posted[0]
    assert "master" in posted[0]


async def test_known_session_no_header():
    posted: list[str] = []

    async def cb(c: str):
        posted.append(c)

    agg = EventAggregator(flush_callback=cb, flush_interval=0.05)
    agg.mark_session_started("s1")
    agg.push(_ev("user_prompt", text="hi"))
    await asyncio.sleep(0.15)
    assert posted
    assert "▶" not in posted[0]


async def test_noise_aggregated_into_one_line():
    events = [_ev("noise", label="Read") for _ in range(5)] + \
             [_ev("noise", label="Grep") for _ in range(3)]
    posted, _ = await _collect(events)
    assert len(posted) == 1
    assert "Read x5" in posted[0]
    assert "Grep x3" in posted[0]


async def test_tool_call_bash_formatted():
    posted, _ = await _collect([_ev("tool_call", name="Bash", summary="ls -la")])
    assert "🔧" in posted[0]
    assert "Bash" in posted[0]
    assert "ls -la" in posted[0]


async def test_tool_call_edit_formatted():
    posted, _ = await _collect([_ev("tool_call", name="Edit", summary="/tmp/foo.py")])
    assert "✏️" in posted[0]
    assert "/tmp/foo.py" in posted[0]


async def test_tool_error_in_code_block():
    posted, _ = await _collect([_ev("tool_error", text="ENOENT: file not found")])
    assert "❌" in posted[0]
    assert "ENOENT" in posted[0]
    assert "```" in posted[0]


async def test_assistant_text_formatted():
    posted, _ = await _collect([_ev("assistant_text", text="Done.")])
    assert "💬" in posted[0]
    assert "Done." in posted[0]


async def test_multiple_sessions_separate_messages():
    events = [_ev("user_prompt", sid="aaa", text="alpha"),
              _ev("user_prompt", sid="bbb", text="beta")]
    posted, _ = await _collect(events)
    assert len(posted) == 2
    combined = "\n".join(posted)
    assert "alpha" in combined
    assert "beta" in combined


async def test_burst_within_window_aggregated():
    posted: list[str] = []

    async def cb(c: str):
        posted.append(c)

    agg = EventAggregator(flush_callback=cb, flush_interval=0.1)
    agg.push(_ev("user_prompt", text="q1"))
    await asyncio.sleep(0.02)
    agg.push(_ev("noise", label="Read"))
    agg.push(_ev("noise", label="Read"))
    await asyncio.sleep(0.2)
    assert len(posted) == 1
    assert "q1" in posted[0]
    assert "Read x2" in posted[0]


async def test_flush_now_drains_pending():
    posted: list[str] = []

    async def cb(c: str):
        posted.append(c)

    agg = EventAggregator(flush_callback=cb, flush_interval=5.0)
    agg.push(_ev("user_prompt", text="urgent"))
    await agg.flush_now()
    assert posted and "urgent" in posted[0]


async def test_flush_callback_exception_does_not_kill_aggregator():
    called = []

    async def cb(c: str):
        called.append(c)
        raise RuntimeError("boom")

    agg = EventAggregator(flush_callback=cb, flush_interval=0.05)
    agg.push(_ev("user_prompt", text="hi"))
    await asyncio.sleep(0.15)
    assert called
    agg.push(_ev("user_prompt", text="hi2"))
    await asyncio.sleep(0.15)
    assert len(called) == 2


def test_split_chunks_under_limit():
    assert _split_chunks("short text") == ["short text"]


def test_split_chunks_breaks_on_newline():
    lines = ["line " + str(i) for i in range(500)]
    text = "\n".join(lines)
    out = _split_chunks(text, limit=200)
    assert all(len(c) <= 200 for c in out)
    rejoined = "\n".join(out)
    for line in lines:
        assert line in rejoined


def test_split_chunks_handles_oversize_line():
    line = "x" * 5000
    out = _split_chunks(line, limit=1800)
    assert all(len(c) <= 1800 for c in out)
    assert "".join(out) == line


# ---------------------------------------------------------------------------
# Memory bound: _buffers + _sessions_seen must not grow without limit (P2-6)
# ---------------------------------------------------------------------------

def test_sessions_seen_bounded(monkeypatch):
    """Adding more than _MAX_SEEN sessions evicts oldest from the set."""
    from services.vscode_sync import aggregator as agg_mod

    monkeypatch.setattr(agg_mod, "_MAX_SEEN", 5)

    async def cb(content: str):
        pass

    agg = agg_mod.EventAggregator(flush_callback=cb, flush_interval=10.0)
    for i in range(20):
        agg._mark_seen(f"s{i}")
    assert len(agg._sessions_seen) == 5
    assert len(agg._sessions_seen_q) == 5
    # Oldest should be gone
    assert "s0" not in agg._sessions_seen
    # Most recent should be present
    assert "s19" in agg._sessions_seen


async def test_buffers_lru_evicts_when_over_cap(monkeypatch):
    """push() evicts oldest pending buffer when over MAX_BUFFERS."""
    from services.vscode_sync import aggregator as agg_mod

    monkeypatch.setattr(agg_mod, "_MAX_BUFFERS", 3)

    async def cb(content: str):
        pass

    agg = agg_mod.EventAggregator(flush_callback=cb, flush_interval=10.0)
    try:
        # 5 distinct sessions, cap=3 → oldest 2 evicted, newest 3 kept
        for i in range(5):
            agg.push(_ev("user_prompt", sid=f"s{i}", text=f"prompt {i}"))
        assert len(agg._buffers) == 3
        assert "s0" not in agg._buffers
        assert "s1" not in agg._buffers
        assert "s2" in agg._buffers
        assert "s3" in agg._buffers
        assert "s4" in agg._buffers
    finally:
        if agg._flush_task and not agg._flush_task.done():
            agg._flush_task.cancel()


async def test_buffers_lru_touch_on_subsequent_push(monkeypatch):
    """A second push for the same session moves it to MRU end."""
    from services.vscode_sync import aggregator as agg_mod

    monkeypatch.setattr(agg_mod, "_MAX_BUFFERS", 3)

    async def cb(content: str):
        pass

    agg = agg_mod.EventAggregator(flush_callback=cb, flush_interval=10.0)
    try:
        agg.push(_ev("user_prompt", sid="s0", text="a"))
        agg.push(_ev("user_prompt", sid="s1", text="b"))
        agg.push(_ev("user_prompt", sid="s2", text="c"))
        # touch s0 → MRU
        agg.push(_ev("user_prompt", sid="s0", text="a2"))
        # push s3 → should evict s1 (now oldest), not s0
        agg.push(_ev("user_prompt", sid="s3", text="d"))
        assert "s0" in agg._buffers
        assert "s1" not in agg._buffers
        assert "s2" in agg._buffers
        assert "s3" in agg._buffers
    finally:
        if agg._flush_task and not agg._flush_task.done():
            agg._flush_task.cancel()

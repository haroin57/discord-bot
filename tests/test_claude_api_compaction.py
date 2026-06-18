"""Tests for services/claude_api/compaction.py (ADR-0010 phase 3).

The compact pipeline mirrors the official claude-cli auto-compact; these
tests pin the pieces reverse-engineered from the binary: summary extraction
(X45), the continuation wrapper (X0$), the keep-boundary walk (o27), and the
failure circuit breaker.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.claude_api.compaction as comp
from services.claude_api.compaction import (
    CONTINUATION_PREAMBLE,
    compact_channel_history,
    compute_keep_boundary,
    estimate_replay_chars,
    extract_summary,
    wrap_summary,
)
from services.session import _apply_compact_summary

CHANNEL = 10**15  # passes _validate_channel_id


@pytest.fixture(autouse=True)
def _reset_breaker():
    comp._compact_failures.clear()
    comp._compact_blocked_until.clear()
    yield
    comp._compact_failures.clear()
    comp._compact_blocked_until.clear()


class TestExtractSummary:
    def test_drops_analysis_and_unwraps_summary(self):
        text = (
            "<analysis>secret scratchpad</analysis>\n"
            "<summary>1. Primary Request:\n   stuff</summary>"
        )
        out = extract_summary(text)
        assert "scratchpad" not in out
        assert out.startswith("Summary:")
        assert "Primary Request" in out

    def test_backslashes_in_summary_survive(self):
        # re.sub with the body as replacement would mangle \g / \1 sequences.
        text = r"<summary>path C:\games and regex \1 \g<name></summary>"
        out = extract_summary(text)
        assert r"C:\games" in out
        assert r"\1" in out

    def test_no_tags_passes_through(self):
        assert extract_summary("plain text") == "plain text"

    def test_blank_runs_squeezed(self):
        out = extract_summary("a\n\n\n\nb")
        assert out == "a\nb"


class TestWrapSummary:
    def test_full_wrapper(self):
        out = wrap_summary(
            "<summary>body</summary>",
            transcript_path="/tmp/x.jsonl",
            kept_verbatim=True,
        )
        assert out.startswith(CONTINUATION_PREAMBLE)
        assert "read the full transcript at: /tmp/x.jsonl" in out
        assert out.endswith("Recent messages are preserved verbatim.")

    def test_minimal_wrapper(self):
        out = wrap_summary("body", transcript_path=None, kept_verbatim=False)
        assert out.startswith(CONTINUATION_PREAMBLE)
        assert "full transcript" not in out
        assert "preserved verbatim" not in out


def _row(role, chars, **extra):
    return {"role": role, "content": "x" * chars, **extra}


class TestComputeKeepBoundary:
    def test_walks_back_until_budget_and_aligns_to_user(self):
        rows = [
            _row("user", 100), _row("assistant", 100),
            _row("user", 100), _row("assistant", 100),
            _row("user", 100), _row("assistant", 100),
        ]
        # 150 chars budget -> last 2 rows reach it; aligned back to the
        # user row at index 4.
        assert compute_keep_boundary(rows, 150) == 4

    def test_keeps_half_when_budget_swallows_everything(self):
        rows = [
            _row("user", 10), _row("assistant", 10),
            _row("user", 10), _row("assistant", 10),
        ]
        b = compute_keep_boundary(rows, 10_000)
        assert 0 < b < len(rows)
        assert rows[b]["role"] == "user"

    def test_empty(self):
        assert compute_keep_boundary([], 100) == 0


class TestEstimateReplayChars:
    def test_counts_str_and_list_content(self):
        msgs = [
            {"role": "user", "content": "abcd"},
            {"role": "assistant", "content": [{"type": "text", "text": "ef"}]},
        ]
        total = estimate_replay_chars(msgs)
        assert total > 4  # str content + serialized list content


class TestApplyCompactSummary:
    def test_collapses_at_latest_summary(self):
        rows = [
            _row("user", 5), _row("assistant", 5),       # summarized away
            _row("user", 5), _row("assistant", 5),       # kept verbatim
            {"role": "user", "content": "SUMMARY",
             "is_compact_summary": True, "keep_tail_rows": 2},
            _row("user", 5),                              # after summary
        ]
        out = _apply_compact_summary(rows)
        assert out[0]["content"] == "SUMMARY"
        assert len(out) == 4  # summary + 2 kept + 1 after
        assert out[1] is rows[2] and out[2] is rows[3] and out[3] is rows[5]

    def test_no_summary_row_is_noop(self):
        rows = [_row("user", 5), _row("assistant", 5)]
        assert _apply_compact_summary(rows) is rows


def _fake_client(response_text):
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=response_text)],
    )
    # Compaction calls the beta namespace (effort/output_config live there).
    client = SimpleNamespace(
        beta=SimpleNamespace(
            messages=SimpleNamespace(create=AsyncMock(return_value=resp)),
        ),
    )
    return client


def _patch_rows(monkeypatch, rows):
    monkeypatch.setattr(comp, "load_compaction_rows", lambda cid: rows)


class TestCompactChannelHistory:
    @pytest.mark.asyncio
    async def test_appends_summary_row_on_success(self, monkeypatch):
        rows = [_row("user", 60_000), _row("assistant", 80_000),
                _row("user", 60_000), _row("assistant", 30_000)]
        _patch_rows(monkeypatch, rows)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 100_000)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_KEEP_CHARS", 40_000)
        appended = []
        monkeypatch.setattr(comp, "append_jsonl", lambda cid, e: appended.append(e))
        client = _fake_client("<analysis>a</analysis><summary>the summary</summary>")

        ok = await compact_channel_history(CHANNEL, client)
        assert ok is True
        assert len(appended) == 1
        row = appended[0]
        assert row["is_compact_summary"] is True
        assert row["role"] == "user"
        assert CONTINUATION_PREAMBLE in row["content"]
        assert "the summary" in row["content"]
        assert row["keep_tail_rows"] >= 1
        # success resets breaker state
        assert comp._compact_failures == {}
        # recursive call ran at Opus max effort (the whole point of the
        # quality dial) — output_config.effort must reach the wire.
        _, kwargs = client.beta.messages.create.call_args
        assert kwargs["output_config"] == {"effort": comp.HISTORY_COMPACT_EFFORT}
        assert kwargs["model"] == comp.HISTORY_COMPACT_MODEL
        assert "effort-2025-11-24" in kwargs["betas"]

    @pytest.mark.asyncio
    async def test_below_trigger_is_noop(self, monkeypatch):
        _patch_rows(monkeypatch, [_row("user", 10)])
        client = _fake_client("<summary>s</summary>")
        assert await compact_channel_history(CHANNEL, client) is False
        client.beta.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_summary_tag_fails_and_counts(self, monkeypatch):
        rows = [_row("user", 150_000), _row("assistant", 100_000),
                _row("user", 10_000), _row("assistant", 5_000),
                _row("user", 5_000)]
        _patch_rows(monkeypatch, rows)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 100_000)
        client = _fake_client("no tags here")
        assert await compact_channel_history(CHANNEL, client) is False
        assert comp._compact_failures[CHANNEL] == 1

    @pytest.mark.asyncio
    async def test_api_error_fails_and_breaker_trips(self, monkeypatch):
        rows = [_row("user", 150_000), _row("assistant", 100_000),
                _row("user", 10_000), _row("assistant", 5_000),
                _row("user", 5_000)]
        _patch_rows(monkeypatch, rows)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 100_000)
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError("boom")),
        )))
        for _ in range(comp.MAX_CONSECUTIVE_COMPACT_FAILURES):
            assert await compact_channel_history(CHANNEL, client) is False
        assert comp._compact_blocked_until.get(CHANNEL, 0) > time.monotonic()
        # breaker open -> no further API calls
        calls_before = client.beta.messages.create.await_count
        assert await compact_channel_history(CHANNEL, client) is False
        assert client.beta.messages.create.await_count == calls_before

    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(comp, "HISTORY_COMPACT_ENABLED", False)
        client = _fake_client("<summary>s</summary>")
        assert await compact_channel_history(CHANNEL, client) is False


class TestForceBypass:
    """Audit #3/#7: force=True must skip the char-based trigger gate."""

    @pytest.mark.asyncio
    async def test_force_compacts_below_char_trigger(self, monkeypatch):
        rows = [_row("user", 500), _row("assistant", 500),
                _row("user", 500), _row("assistant", 500)]
        _patch_rows(monkeypatch, rows)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 10**9)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_KEEP_CHARS", 600)
        appended: list[dict] = []
        monkeypatch.setattr(comp, "append_jsonl", lambda cid, e: appended.append(e))
        client = _fake_client("<summary>compact body</summary>")
        ok = await compact_channel_history(CHANNEL, client, force=True)
        assert ok is True
        assert appended and appended[0]["is_compact_summary"] is True

    @pytest.mark.asyncio
    async def test_no_force_below_trigger_is_noop(self, monkeypatch):
        _patch_rows(monkeypatch, [_row("user", 500)])
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 10**9)
        client = _fake_client("<summary>x</summary>")
        assert await compact_channel_history(CHANNEL, client) is False


class TestRapidRefillBreaker:
    """Audit #19: 3 consecutive proactive compactions = thrashing → cooldown."""

    @pytest.fixture(autouse=True)
    def _reset_refills(self):
        comp._rapid_refills.clear()
        yield
        comp._rapid_refills.clear()

    def test_trips_after_three_consecutive(self):
        for _ in range(comp.MAX_CONSECUTIVE_RAPID_REFILLS):
            comp.record_proactive_compaction(CHANNEL)
        assert comp._compact_blocked_until.get(CHANNEL, 0) > time.monotonic()

    def test_reset_between_requests_prevents_trip(self):
        comp.record_proactive_compaction(CHANNEL)
        comp.record_proactive_compaction(CHANNEL)
        comp.reset_rapid_refill(CHANNEL)
        comp.record_proactive_compaction(CHANNEL)
        comp.record_proactive_compaction(CHANNEL)
        assert comp._compact_blocked_until.get(CHANNEL, 0) == 0

    @pytest.mark.asyncio
    async def test_open_breaker_blocks_even_forced(self, monkeypatch):
        for _ in range(comp.MAX_CONSECUTIVE_RAPID_REFILLS):
            comp.record_proactive_compaction(CHANNEL)
        _patch_rows(monkeypatch, [_row("user", 10**6)])
        client = _fake_client("<summary>x</summary>")
        assert await compact_channel_history(CHANNEL, client, force=True) is False

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
        assert ok is not None
        assert len(appended) == 1
        row = appended[0]
        assert row["is_compact_summary"] is True
        assert row["role"] == "user"
        assert CONTINUATION_PREAMBLE in row["content"]
        assert "the summary" in row["content"]
        assert row["keep_tail_rows"] >= 1
        # success resets breaker state
        assert comp._compact_failures == {}
        # the configured effort dial must reach the wire (default "medium"
        # since the 2026-07-08 runtime optimization; env-tunable either way).
        _, kwargs = client.beta.messages.create.call_args
        assert kwargs["output_config"] == {"effort": comp.HISTORY_COMPACT_EFFORT}
        assert kwargs["model"] == comp.HISTORY_COMPACT_MODEL
        assert "effort-2025-11-24" in kwargs["betas"]

    def test_instructions_request_summary_only(self):
        # Runtime optimization (2026-07-08): the throwaway <analysis>
        # scratchpad is no longer requested — the response must be a single
        # <summary> block, with the checklist done in private reasoning.
        assert "single <summary> block" in comp.COMPACT_INSTRUCTIONS
        assert "wrap your analysis in <analysis> tags" not in comp.COMPACT_INSTRUCTIONS
        # extract_summary keeps stripping a stray <analysis> defensively.
        out = extract_summary("<analysis>x</analysis><summary>s</summary>")
        assert "x" not in out

    @pytest.mark.asyncio
    async def test_below_trigger_is_noop(self, monkeypatch):
        _patch_rows(monkeypatch, [_row("user", 10)])
        client = _fake_client("<summary>s</summary>")
        assert await compact_channel_history(CHANNEL, client) is None
        client.beta.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_summary_tag_fails_and_counts(self, monkeypatch):
        rows = [_row("user", 150_000), _row("assistant", 100_000),
                _row("user", 10_000), _row("assistant", 5_000),
                _row("user", 5_000)]
        _patch_rows(monkeypatch, rows)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 100_000)
        client = _fake_client("no tags here")
        assert await compact_channel_history(CHANNEL, client) is None
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
            assert await compact_channel_history(CHANNEL, client) is None
        assert comp._compact_blocked_until.get(CHANNEL, 0) > time.monotonic()
        # breaker open -> no further API calls
        calls_before = client.beta.messages.create.await_count
        assert await compact_channel_history(CHANNEL, client) is None
        assert client.beta.messages.create.await_count == calls_before

    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(comp, "HISTORY_COMPACT_ENABLED", False)
        client = _fake_client("<summary>s</summary>")
        assert await compact_channel_history(CHANNEL, client) is None


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
        assert ok is not None
        assert appended and appended[0]["is_compact_summary"] is True

    @pytest.mark.asyncio
    async def test_no_force_below_trigger_is_noop(self, monkeypatch):
        _patch_rows(monkeypatch, [_row("user", 500)])
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 10**9)
        client = _fake_client("<summary>x</summary>")
        assert await compact_channel_history(CHANNEL, client) is None


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
        assert await compact_channel_history(CHANNEL, client, force=True) is None


class TestApiMessagesFallback:
    """content 空のツール中心ターンでも api_messages から要約入力を復元する。"""

    def _tool_heavy_row(self):
        return {
            "role": "assistant",
            "content": "",  # 表示テキストなし（ツールだけ使ったターン）
            "api_messages": [
                {"role": "assistant", "content": [
                    {"type": "text", "text": "ファイルを確認します"},
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/x"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": "みっしりしたツール出力"},
                ]},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "確認完了、問題ありません"},
                ]},
            ],
        }

    def test_fallback_recovers_text_blocks(self):
        text = comp._api_messages_text_fallback(self._tool_heavy_row())
        assert "ファイルを確認します" in text
        assert "確認完了" in text
        # tool_use / tool_result の中身は bulk なので拾わない
        assert "みっしりしたツール出力" not in text

    def test_flatten_includes_fallback_content(self):
        rows = [
            {"role": "user", "content": "調べて"},
            self._tool_heavy_row(),
        ]
        out = comp._flatten_rows_for_summary(rows, max_chars=10_000)
        assert "調べて" in out
        assert "ファイルを確認します" in out
        assert "[assistant (かなえ)]" in out

    def test_no_api_messages_still_skipped(self):
        rows = [{"role": "assistant", "content": "", "api_messages": []}]
        assert comp._flatten_rows_for_summary(rows, max_chars=1000) == ""

    def test_fallback_is_capped(self):
        row = {
            "role": "assistant", "content": "",
            "api_messages": [
                {"role": "assistant",
                 "content": [{"type": "text", "text": "長い" * 5000}]},
            ],
        }
        text = comp._api_messages_text_fallback(row)
        assert len(text) <= comp._API_MSG_FALLBACK_CHARS + 20  # cap + 省略記号


def _ts_row(role, chars, ts):
    return {"role": role, "content": "x" * chars, "timestamp": ts}


class TestBackgroundCompaction:
    """2026-07-08 runtime optimization: boundary compaction runs off the
    critical path, and the summary row's keep_tail_rows is re-anchored at
    append time so rows appended during the minutes-long summary call land in
    the verbatim tail instead of being dropped from replay."""

    @pytest.fixture(autouse=True)
    def _reset_bg_state(self):
        comp._bg_tasks.clear()
        comp._compact_locks.clear()
        yield
        comp._bg_tasks.clear()
        comp._compact_locks.clear()

    @pytest.mark.asyncio
    async def test_spawns_once_per_channel(self, monkeypatch):
        import asyncio
        release = asyncio.Event()
        started = asyncio.Event()

        async def _slow_compact(cid, client, cb=None, *, force=False):
            started.set()
            await release.wait()
            return None

        monkeypatch.setattr(comp, "compact_channel_history", _slow_compact)

        assert comp.start_background_compaction(CHANNEL, object()) is True
        await started.wait()
        # Second dispatch while the first is in flight must be refused.
        assert comp.start_background_compaction(CHANNEL, object()) is False
        release.set()
        await comp._bg_tasks[CHANNEL] if CHANNEL in comp._bg_tasks else None
        # After completion the slot frees up.
        for _ in range(10):
            if CHANNEL not in comp._bg_tasks:
                break
            await asyncio.sleep(0)
        assert comp.start_background_compaction(CHANNEL, object()) is True
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_concurrent_append_recomputes_tail(self, monkeypatch):
        """Rows appended while the summary call is in flight must be counted
        into keep_tail_rows — the stale pre-call count would drop real tail
        rows from the replay view."""
        rows_v1 = [
            _ts_row("user", 60_000, "t1"), _ts_row("assistant", 80_000, "t2"),
            _ts_row("user", 60_000, "t3"), _ts_row("assistant", 30_000, "t4"),
        ]
        # Two rows land while the LLM call runs.
        rows_v2 = rows_v1 + [
            _ts_row("user", 10, "t5"), _ts_row("assistant", 10, "t6"),
        ]
        calls = {"n": 0}

        def _fake_load(cid):
            calls["n"] += 1
            return rows_v1 if calls["n"] == 1 else rows_v2

        monkeypatch.setattr(comp, "load_compaction_rows", _fake_load)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 100_000)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_KEEP_CHARS", 40_000)
        appended: list[dict] = []
        monkeypatch.setattr(comp, "append_jsonl", lambda cid, e: appended.append(e))
        client = _fake_client("<summary>the summary</summary>")

        ok = await compact_channel_history(CHANNEL, client)
        assert ok is not None
        assert calls["n"] == 2  # snapshot + append-time re-anchor
        row = appended[0]
        # Original tail (2 rows) + 2 concurrently-appended rows.
        assert row["keep_tail_rows"] == 4
        assert ok.kept_rows == 4

    @pytest.mark.asyncio
    async def test_anchor_missing_aborts_append(self, monkeypatch):
        """If the summarized boundary row vanished from the window mid-run
        (head-trim), the summary is discarded rather than appended with a
        misaligned tail — and the failure breaker is NOT bumped (the LLM call
        itself succeeded)."""
        rows_v1 = [
            _ts_row("user", 60_000, "t1"), _ts_row("assistant", 80_000, "t2"),
            _ts_row("user", 60_000, "t3"), _ts_row("assistant", 30_000, "t4"),
        ]
        rows_v2 = [  # boundary row (t2) gone entirely
            _ts_row("user", 60_000, "t3"), _ts_row("assistant", 30_000, "t4"),
        ]
        calls = {"n": 0}

        def _fake_load(cid):
            calls["n"] += 1
            return rows_v1 if calls["n"] == 1 else rows_v2

        monkeypatch.setattr(comp, "load_compaction_rows", _fake_load)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 100_000)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_KEEP_CHARS", 40_000)
        appended: list[dict] = []
        monkeypatch.setattr(comp, "append_jsonl", lambda cid, e: appended.append(e))
        client = _fake_client("<summary>the summary</summary>")

        assert await compact_channel_history(CHANNEL, client) is None
        assert appended == []
        assert comp._compact_failures == {}

    @pytest.mark.asyncio
    async def test_duplicate_tail_rows_do_not_confuse_anchor(self, monkeypatch):
        """A tail row value-identical to the boundary row (same second, same
        role, same text) must not be mistaken for the anchor — the search is
        capped at the original boundary index."""
        dup = {"role": "assistant", "content": "x" * 500, "timestamp": "same"}
        rows = [
            _ts_row("user", 500, "t1"), dict(dup),
            _ts_row("user", 500, "t3"), dict(dup),
        ]
        monkeypatch.setattr(comp, "load_compaction_rows", lambda cid: rows)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_TRIGGER_CHARS", 10**9)
        monkeypatch.setattr(comp, "HISTORY_COMPACT_KEEP_CHARS", 600)
        appended: list[dict] = []
        monkeypatch.setattr(comp, "append_jsonl", lambda cid, e: appended.append(e))
        client = _fake_client("<summary>compact body</summary>")

        ok = await compact_channel_history(CHANNEL, client, force=True)
        assert ok is not None
        # boundary=2 -> tail is rows[2:]; the duplicate at index 3 must not
        # shrink the tail to 0.
        assert appended[0]["keep_tail_rows"] == 2

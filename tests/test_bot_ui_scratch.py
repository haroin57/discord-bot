"""Tests for bot_ui.LiveRenderer's Scratchpad live-preview (scratch_stream_preview
/ scratch_finalize). tool_loop.py forwards Scratchpad's in-progress input_json
snapshots here so the write is visible instead of freezing streaming — see
run_agent_loop's "input_json" / "content_block_stop" handling."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot_ui


def _make_renderer():
    message = MagicMock()
    message.channel.send = AsyncMock()
    status = MagicMock()
    status.reposition = AsyncMock()
    renderer = bot_ui.LiveRenderer(
        message, status,
        track_task=lambda coro, name=None: coro,
        job=MagicMock(),
        split_message=lambda text, limit, numbered=False: [text],
    )
    return renderer, message, status


class TestScratchStreamPreview:
    @pytest.mark.asyncio
    async def test_first_call_sends_a_new_message(self):
        renderer, message, status = _make_renderer()
        await renderer.scratch_stream_preview("進捗", "調べ中", "write")
        message.channel.send.assert_awaited_once()
        payload = message.channel.send.await_args.args[0]
        assert "進捗" in payload
        assert "調べ中" in payload
        status.reposition.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_throttled_within_interval(self):
        renderer, message, status = _make_renderer()
        await renderer.scratch_stream_preview("進捗", "調べ中", "write")
        # Immediately called again — within STREAM_EDIT_INTERVAL_S, must skip.
        await renderer.scratch_stream_preview("進捗", "調べ中…続き", "write")
        message.channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_edits_existing_message_after_throttle_window(self):
        renderer, message, status = _make_renderer()
        await renderer.scratch_stream_preview("進捗", "調べ中", "write")
        sent_msg = renderer._scratch_state["msg"]
        sent_msg.edit = AsyncMock()
        # Force the throttle window to have elapsed.
        renderer._scratch_state["last_edit"] = (
            time.monotonic() - renderer.STREAM_EDIT_INTERVAL_S - 1
        )
        await renderer.scratch_stream_preview("進捗", "調べ中…完了", "write")
        sent_msg.edit.assert_awaited_once()
        payload = sent_msg.edit.await_args.kwargs["content"]
        assert "調べ中…完了" in payload
        message.channel.send.assert_awaited_once()  # still only the first send


class TestScratchFinalize:
    @pytest.mark.asyncio
    async def test_noop_when_nothing_streamed(self):
        renderer, message, status = _make_renderer()
        await renderer.scratch_finalize()
        message.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_collapses_to_one_line_subtext(self):
        renderer, message, status = _make_renderer()
        await renderer.scratch_stream_preview("進捗", "調べ中", "write")
        sent_msg = renderer._scratch_state["msg"]
        sent_msg.edit = AsyncMock()
        await renderer.scratch_finalize()
        sent_msg.edit.assert_awaited_once()
        payload = sent_msg.edit.await_args.kwargs["content"]
        assert payload == "-# 💭 メモ「進捗」"
        # section is cleared so a later write starts a fresh preview block.
        assert renderer._scratch_state["section"] is None

    @pytest.mark.asyncio
    async def test_multiple_sections_accumulate_in_summary(self):
        renderer, message, status = _make_renderer()
        await renderer.scratch_stream_preview("進捗", "調べ中", "write")
        sent_msg = renderer._scratch_state["msg"]
        sent_msg.edit = AsyncMock()
        await renderer.scratch_finalize()

        renderer._scratch_state["last_edit"] = 0.0
        await renderer.scratch_stream_preview("発見", "見つけた", "write")
        await renderer.scratch_finalize()

        payload = sent_msg.edit.await_args.kwargs["content"]
        assert payload == "-# 💭 メモ「進捗」\n-# 💭 メモ「発見」"

"""Tests for bot_ui.LiveRenderer's plan/verify live-preview (plan_stream_preview
/ plan_finalize). tool_loop.py forwards the model's thinking (kind="計画") and
the verifier's narration (kind="検証") here so deliberate planning/verification
isn't a silent black box — see run_agent_loop's thinking_delta handling and
entry.py's verify_draft wiring."""

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


class TestPlanStreamPreview:
    @pytest.mark.asyncio
    async def test_first_call_sends_a_new_message(self):
        renderer, message, status = _make_renderer()
        await renderer.plan_stream_preview("いま何を計画中か", "計画")
        message.channel.send.assert_awaited_once()
        payload = message.channel.send.await_args.args[0]
        assert "🧠" in payload
        assert "計画中" in payload
        assert "いま何を計画中か" in payload
        status.reposition.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verify_kind_uses_search_emoji(self):
        renderer, message, status = _make_renderer()
        await renderer.plan_stream_preview("ドラフトを検証中", "検証")
        payload = message.channel.send.await_args.args[0]
        assert "🔎" in payload
        assert "検証中" in payload

    @pytest.mark.asyncio
    async def test_throttled_within_interval(self):
        renderer, message, status = _make_renderer()
        await renderer.plan_stream_preview("A", "計画")
        # Immediately again — within STREAM_EDIT_INTERVAL_S, must skip.
        await renderer.plan_stream_preview("A…続き", "計画")
        message.channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_edits_existing_after_throttle_window(self):
        renderer, message, status = _make_renderer()
        await renderer.plan_stream_preview("A", "計画")
        sent_msg = renderer._plan_state["msg"]
        sent_msg.edit = AsyncMock()
        renderer._plan_state["last_edit"] = (
            time.monotonic() - renderer.STREAM_EDIT_INTERVAL_S - 1
        )
        await renderer.plan_stream_preview("A→B→完了", "計画")
        sent_msg.edit.assert_awaited_once()
        payload = sent_msg.edit.await_args.kwargs["content"]
        assert "A→B→完了" in payload
        message.channel.send.assert_awaited_once()  # still only the first send


class TestPlanFinalize:
    @pytest.mark.asyncio
    async def test_noop_when_nothing_streamed(self):
        renderer, message, status = _make_renderer()
        await renderer.plan_finalize()
        message.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_collapses_to_one_line_subtext(self):
        renderer, message, status = _make_renderer()
        await renderer.plan_stream_preview("考え中", "計画")
        sent_msg = renderer._plan_state["msg"]
        sent_msg.edit = AsyncMock()
        await renderer.plan_finalize()
        sent_msg.edit.assert_awaited_once()
        payload = sent_msg.edit.await_args.kwargs["content"]
        assert payload == "-# 🧠 計画"
        # kind cleared so a later block starts a fresh preview.
        assert renderer._plan_state["kind"] is None

    @pytest.mark.asyncio
    async def test_repeated_same_kind_folds_into_count(self):
        renderer, message, status = _make_renderer()
        for _ in range(3):
            renderer._plan_state["last_edit"] = 0.0
            await renderer.plan_stream_preview("考え中", "計画")
            sent_msg = renderer._plan_state["msg"]
            sent_msg.edit = AsyncMock()
            await renderer.plan_finalize()
        payload = sent_msg.edit.await_args.kwargs["content"]
        assert payload == "-# 🧠 計画 ×3"

    @pytest.mark.asyncio
    async def test_plan_and_verify_render_two_lines(self):
        renderer, message, status = _make_renderer()
        # 計画 block
        await renderer.plan_stream_preview("計画中", "計画")
        sent_msg = renderer._plan_state["msg"]
        sent_msg.edit = AsyncMock()
        await renderer.plan_finalize()
        # 検証 block reuses the same preview message
        renderer._plan_state["last_edit"] = 0.0
        await renderer.plan_stream_preview("検証中", "検証")
        await renderer.plan_finalize()
        payload = sent_msg.edit.await_args.kwargs["content"]
        assert payload == "-# 🧠 計画\n-# 🔎 検証"

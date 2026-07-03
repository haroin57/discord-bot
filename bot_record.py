"""Mem0 per-turn recording (R4).

Extracted from bot.on_message: batch turn accumulation + flush into the
long-term (Mem0) store. Only admin callers reach here — non-admin writes
would let an attacker plant persistent "facts" that later sessions inject
into the prompt (C-7 poisoning). on_message keeps that admin gate around
the single record_turn() call.

Collaborators that tests patch as bot-module attributes (add_memories,
_track_task, load_history_channel, _is_force_record_request) are injected
by bot.py at call time, so the patched object is the one that runs. The
services.mem0.batch helpers are lazy-imported here on each call: tests
patch them at their source (patch("services.mem0.batch.append_turn") ...),
and importing them inside the function keeps that patch effective.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import discord

from config import MEM0_LEGACY_CONTEXT, STATE_DIR

log = logging.getLogger(__name__)


async def record_turn(
    *,
    message: discord.Message,
    raw_user_text: str,
    response_text: str,
    mem0_user_id: str,
    conv_context: str,
    add_memories: Callable[..., Awaitable],
    track_task: Callable,
    load_history_channel: Callable,
    is_force_record_request: Callable[[str], bool],
) -> None:
    """Accumulate this admin turn into the Mem0 batch and flush when full.

    Behaviour is identical to the inline block it replaces: append the turn,
    decide whether to flush (batch full OR explicit "覚えて" request), build
    the optional legacy context hint, and fire a fire-and-forget background
    store via track_task when flushing.
    """
    from services.mem0.batch import (
        append_turn as _batch_append_turn,
        pop_batch as _batch_pop,
        build_batch_summary as _build_batch_summary,
        BATCH_SIZE as _BATCH_SIZE,
    )

    _turn_count = await asyncio.to_thread(
        _batch_append_turn, STATE_DIR, message.channel.id,
        raw_user_text, response_text,
    )

    _should_flush = (
        _turn_count >= _BATCH_SIZE
        or is_force_record_request(raw_user_text)
    )

    # P1 (2026-05-12): system-message経路は廃止して複数人混入を遮断。
    _ctx_for_mem0 = ""
    if MEM0_LEGACY_CONTEXT:
        _ctx_for_mem0 = conv_context or ""
        if not _ctx_for_mem0:
            try:
                recent = await asyncio.to_thread(load_history_channel, message.channel.id)
                if recent:
                    _ctx_for_mem0 = "\n".join(
                        f"{m['role']}: {m['content'][:300]}" for m in recent[-10:]
                    )
            except Exception:
                pass

    if _should_flush:
        _batch_turns, _ = await asyncio.to_thread(
            _batch_pop, STATE_DIR, message.channel.id,
        )

        async def _mem0_batch_store():
            try:
                ctx_hint = _ctx_for_mem0 if MEM0_LEGACY_CONTEXT else ""
                summary_text = _build_batch_summary(_batch_turns)
                log.info(
                    "Mem0 batch ingest: uid=%s turns=%d chars=%d",
                    mem0_user_id, len(_batch_turns), len(summary_text),
                )
                await add_memories(
                    user_text=summary_text,
                    assistant_text="",
                    user_id=mem0_user_id,
                    context_hint=ctx_hint,
                    metadata={
                        "channel_id": str(message.channel.id),
                        "channel_name": getattr(message.channel, "name", "DM"),
                        "batch_size": str(len(_batch_turns)),
                    },
                )

            except Exception as exc:
                log.warning(
                    "Mem0 batch store failed: %s (user=%s, channel=%s)",
                    exc, mem0_user_id, message.channel.id,
                )

        track_task(_mem0_batch_store(), name="mem0_batch_store")
    else:
        log.debug(
            "Mem0 batch: ch=%s turns=%d/%d (waiting)",
            message.channel.id, _turn_count, _BATCH_SIZE,
        )

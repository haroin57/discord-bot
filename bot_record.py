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
from contextlib import suppress
import json
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
    request_id: str | None = None,
) -> None:
    """Accumulate this admin turn into the Mem0 batch and flush when full.

    Behaviour is identical to the inline block it replaces: append the turn,
    decide whether to flush (batch full OR explicit "覚えて" request), build
    the optional legacy context hint, and fire a fire-and-forget background
    store via track_task when flushing.
    """
    from services.mem0 import batch as batch_module

    if hasattr(batch_module, "claim_batch"):
        await _record_alpha_turn(
            batch=batch_module, message=message, raw_user_text=raw_user_text,
            response_text=response_text, mem0_user_id=mem0_user_id,
            add_memories=add_memories, track_task=track_task,
            is_force_record_request=is_force_record_request, request_id=request_id,
        )
        return

    from services.mem0.batch import (
        append_turn as _batch_append_turn,
        pop_batch as _batch_pop,
        build_batch_summary as _build_batch_summary,
        BATCH_SIZE as _BATCH_SIZE,
    )

    append_kwargs = {"request_id": request_id} if request_id else {}
    _turn_count = await asyncio.to_thread(
        _batch_append_turn,
        STATE_DIR,
        message.channel.id,
        raw_user_text,
        response_text,
        **append_kwargs,
    )

    _should_flush = _turn_count >= _BATCH_SIZE or is_force_record_request(raw_user_text)

    # P1 (2026-05-12): system-message経路は廃止して複数人混入を遮断。
    _ctx_for_mem0 = ""
    if MEM0_LEGACY_CONTEXT:
        _ctx_for_mem0 = conv_context or ""
        if not _ctx_for_mem0:
            try:
                recent = await asyncio.to_thread(
                    load_history_channel, message.channel.id
                )
                if recent:
                    _ctx_for_mem0 = "\n".join(
                        f"{m['role']}: {m['content'][:300]}" for m in recent[-10:]
                    )
            except Exception:
                pass

    if _should_flush:
        _batch_turns, _ = await asyncio.to_thread(
            _batch_pop,
            STATE_DIR,
            message.channel.id,
        )

        async def _mem0_batch_store():
            try:
                ctx_hint = _ctx_for_mem0 if MEM0_LEGACY_CONTEXT else ""
                summary_text = _build_batch_summary(_batch_turns)
                log.info(
                    "Mem0 batch ingest: uid=%s turns=%d chars=%d",
                    mem0_user_id,
                    len(_batch_turns),
                    len(summary_text),
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
                    exc,
                    mem0_user_id,
                    message.channel.id,
                )

        track_task(_mem0_batch_store(), name="mem0_batch_store")
    else:
        log.debug(
            "Mem0 batch: ch=%s turns=%d/%d (waiting)",
            message.channel.id,
            _turn_count,
            _BATCH_SIZE,
        )


async def _record_alpha_turn(*, batch, message, raw_user_text, response_text,
                             mem0_user_id, add_memories, track_task,
                             is_force_record_request, request_id):
    """Persist attributable turns; acknowledge only a successful provider result.

    A failure remains on disk for the next turn/retry. Existing unknown-owner
    batches are deliberately left untouched. Shared conversation context is a
    separate read policy and is not narrowed by speaker-aware fact storage.
    """
    channel_id = message.channel.id
    author_id = str(getattr(message.author, "id", "") or mem0_user_id)
    created_at = getattr(message, "created_at", None)
    message_id = getattr(message, "id", None)
    if created_at is None and type(message_id) is int and 10**14 <= message_id < 2**64:
        # Durable outbox replay reconstructs id/channel/author only. Discord
        # snowflakes retain the original event time, unlike the retry clock.
        created_at = discord.utils.snowflake_time(message_id)
    timestamp = created_at.isoformat() if created_at is not None else None
    source_id = request_id or str(getattr(message, "id", "") or "")
    force_flush = is_force_record_request(raw_user_text)
    await asyncio.to_thread(
        batch.append_turn, STATE_DIR, channel_id, raw_user_text, response_text,
        request_id=source_id, user_id=mem0_user_id, author_id=author_id,
        source_timestamp=timestamp, message_id=str(getattr(message, "id", "") or ""),
        force_flush=force_flush,
    )
    ready = await asyncio.to_thread(
        batch.has_ready_batch, STATE_DIR, channel_id, user_id=mem0_user_id,
    )
    if not ready:
        return

    async def store_one(lease):
        lost_lease = asyncio.Event()

        async def renew():
            while True:
                await asyncio.sleep(30)
                try:
                    ok = await asyncio.to_thread(batch.renew_batch, STATE_DIR, lease)
                except Exception:
                    ok = False
                if not ok:
                    lost_lease.set()
                    return

        heartbeat = asyncio.create_task(renew(), name="mem0_batch_lease_renew")
        acknowledged = False
        error = "cancelled or incomplete ingest"
        try:
            # Keep all turns/roles intact. Extraction handles bounded chunks;
            # assistant statements remain context, not independent user facts.
            messages = []
            for turn in lease.turns:
                messages.extend([
                    {"role": "user", "content": turn.get("user", "")},
                    {"role": "assistant", "content": turn.get("assistant", "")},
                ])
            result = await add_memories(
                messages=messages, user_id=lease.user_id,
                metadata={
                    "channel_id": str(channel_id),
                    "channel_name": getattr(message.channel, "name", "DM"),
                    "batch_id": lease.batch_id,
                    "batch_size": str(len(lease.turns)),
                    "source": "discord_batch",
                    "source_author_id": lease.turns[0].get("author_id", "") if lease.turns else "",
                    "source_message_ids": json.dumps([
                        t["message_id"] for t in lease.turns if t.get("message_id")
                    ]),
                    "source_timestamp": lease.turns[0].get("ts", "") if lease.turns else "",
                },
            )
            if (not isinstance(result, dict) or result.get("error")
                    or result.get("status") in {"error", "partial"}
                    or result.get("failed")
                    or not (result.get("status") == "ok" or (
                        "status" not in result and isinstance(result.get("results"), list)
                    ))):
                error = "memory provider reported incomplete ingestion"
                return
            if lost_lease.is_set():
                error = "batch lease ownership lost"
                return
            acknowledged = await asyncio.to_thread(batch.ack_batch, STATE_DIR, lease)
            if not acknowledged:
                error = "batch acknowledgement rejected"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = type(exc).__name__
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            if not acknowledged:
                # A failed retry write still leaves the original lease on disk;
                # it becomes recoverable automatically when the lease expires.
                try:
                    await asyncio.shield(asyncio.to_thread(batch.retry_batch, STATE_DIR, lease, error))
                except Exception as exc:
                    log.warning("Mem0 retry persistence failed: %s", type(exc).__name__)
                log.warning("Mem0 batch retained for retry: channel=%s reason=%s", channel_id, error)
        return acknowledged

    async def store_batch():
        while True:
            lease = await asyncio.to_thread(
                batch.claim_batch, STATE_DIR, channel_id, user_id=mem0_user_id,
            )
            if lease is None:
                # The current owner will drain ready successors after its ACK.
                return
            if not await store_one(lease):
                return  # Never spin on a failing provider.
            ready = await asyncio.to_thread(
                batch.has_ready_batch, STATE_DIR, channel_id, user_id=mem0_user_id,
            )
            if not ready:
                return

    track_task(store_batch(), name="mem0_batch_store")

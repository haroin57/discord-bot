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
import contextvars
import functools
import json
import logging
import math
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

    A failure remains on disk for the next turn or recovery worker. Unknown-owner
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
    await _batch_io(
        batch.append_turn, STATE_DIR, channel_id, raw_user_text, response_text,
        request_id=source_id, user_id=mem0_user_id, author_id=author_id,
        source_timestamp=timestamp, message_id=str(getattr(message, "id", "") or ""),
        force_flush=force_flush,
        channel_name=getattr(message.channel, "name", "DM"),
    )
    ready = await _batch_io(
        batch.has_ready_batch, STATE_DIR, channel_id, user_id=mem0_user_id,
    )
    if not ready:
        return

    track_task(_drain_alpha_owner(
        batch=batch, state_dir=STATE_DIR, channel_id=channel_id, user_id=mem0_user_id,
        channel_name=getattr(message.channel, "name", "DM"), add_memories=add_memories,
    ), name="mem0_batch_store")


async def _batch_io(operation, *args, **kwargs):
    """Drain a real filesystem worker even under all-Task shutdown cancellation."""
    context = contextvars.copy_context()
    worker = asyncio.get_running_loop().run_in_executor(
        None, functools.partial(context.run, operation, *args, **kwargs),
    )
    cancelled = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except Exception:
            break
    if cancelled is not None:
        try:
            worker.result()
        except Exception as exc:
            log.warning("Mem0 batch I/O failed during cancellation: %s", type(exc).__name__)
        raise cancelled
    return worker.result()


async def _drain_alpha_owner(*, batch, state_dir, channel_id, user_id, add_memories,
                             channel_name="", max_batches=None, max_age_seconds=0,
                             retry_base_seconds=0, retry_max_seconds=600):
    """Use one delivery contract for foreground recording and idle recovery."""

    async def store_one(lease):
        from services.mem0.api_contracts import contracts
        lost_lease = asyncio.Event()

        async def renew():
            while True:
                await asyncio.sleep(30)
                try:
                    ok = await _batch_io(batch.renew_batch, state_dir, lease)
                except Exception:
                    ok = False
                if not ok:
                    lost_lease.set()
                    return

        heartbeat = asyncio.create_task(renew(), name="mem0_batch_lease_renew")
        acknowledged = False
        made_progress = False
        error = "cancelled or incomplete ingest"
        try:
            # Keep all turns/roles intact. Extraction handles bounded chunks;
            # assistant statements remain context, not independent user facts.
            messages = []
            for turn in lease.turns:
                source = ("discord:" + str(turn["message_id"]) if turn.get("message_id")
                          else str(turn.get("request_id") or turn.get("id") or ""))
                messages.extend([
                    {"role": "user", "content": turn.get("user", ""),
                     "source_id": source, "author_id": turn.get("author_id", ""),
                     "source_timestamp": turn.get("ts", "")},
                    {"role": "assistant", "content": turn.get("assistant", ""),
                     "source_id": source + ":assistant" if source else ""},
                ])
            result = await add_memories(
                messages=messages, user_id=lease.user_id,
                metadata={
                    "channel_id": str(channel_id),
                    "channel_name": channel_name or (lease.turns[0].get("channel_name", "") if lease.turns else ""),
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
            if contracts.classify_add_outcome(result).status != "ok":
                made_progress = (isinstance(result, dict) and result.get("status") == "partial"
                                 and result.get("error") == "continuation_required"
                                 and result.get("continuation_id") == lease.batch_id
                                 and not result.get("failed")
                                 and type(result.get("next_offset")) is int
                                 and type(result.get("total_chars")) is int
                                 and 0 < result["next_offset"] < result["total_chars"])
                error = "continuation_required" if made_progress else "memory provider reported incomplete ingestion"
                return
            if lost_lease.is_set():
                error = "batch lease ownership lost"
                return
            acknowledged = await _batch_io(batch.ack_batch, state_dir, lease)
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
                    await _batch_io(batch.retry_batch, state_dir, lease, error,
                                    delay_seconds=retry_base_seconds,
                                    max_delay_seconds=retry_max_seconds,
                                    made_progress=made_progress)
                except Exception as exc:
                    log.warning("Mem0 retry persistence failed: %s", type(exc).__name__)
                log.warning("Mem0 batch retained for retry: channel=%s reason=%s", channel_id, error)
        return acknowledged

    completed = 0
    while max_batches is None or completed < max_batches:
        lease = await _batch_io(batch.claim_batch, state_dir, channel_id, user_id=user_id)
        if lease is None:
            return completed
        if not await store_one(lease):
            return completed  # Never spin on a failing provider.
        completed += 1
        ready = await _batch_io(batch.has_ready_batch, state_dir, channel_id,
                               user_id=user_id, max_age_seconds=max_age_seconds)
        if not ready:
            return completed
    return completed


async def recover_alpha_batches_once(*, add_memories, allowed_user_ids,
                                     cursor=None, state_dir=None, max_age_seconds=600,
                                     max_files=100, max_batches=2,
                                     retry_base_seconds=30, retry_max_seconds=600):
    """Recover bounded attributable work without waiting for a new message."""
    from services.mem0 import batch
    if not hasattr(batch, "scan_ready_batches"):
        return {"enabled": False, "completed": 0, "cursor": cursor}
    state_dir = STATE_DIR if state_dir is None else state_dir
    allowed = {str(value) for value in allowed_user_ids}
    scan = await _batch_io(batch.scan_ready_batches, state_dir,
                          allowed_user_ids=allowed, max_age_seconds=max_age_seconds,
                          max_files=max_files, limit=max_batches, cursor=cursor)
    completed = 0
    for channel_id, owner in scan["ready"]:
        if owner not in {str(value) for value in allowed_user_ids}:
            continue  # Honor permission revocation while a prior batch runs.
        completed += await _drain_alpha_owner(
            batch=batch, state_dir=state_dir, channel_id=channel_id, user_id=owner,
            add_memories=add_memories, max_batches=1, max_age_seconds=max_age_seconds,
            retry_base_seconds=retry_base_seconds, retry_max_seconds=retry_max_seconds,
        )
    return {**{key: value for key, value in scan.items() if key != "ready"},
            "enabled": True, "completed": completed, "selected": len(scan["ready"])}


async def alpha_recovery_worker(*, add_memories, allowed_user_ids):
    """Owned by the bot runtime; lease state survives task/process shutdown."""
    from services.mem0 import batch
    if not hasattr(batch, "scan_ready_batches"):
        return
    from llm_mem0.settings import settings
    cursor = None
    while settings.MEM0_BATCH_RECOVERY_ENABLED:
        interval = settings.MEM0_BATCH_RECOVERY_INTERVAL_S
        age = settings.MEM0_BATCH_MAX_AGE_S
        base, maximum = settings.MEM0_BATCH_RETRY_BASE_S, settings.MEM0_BATCH_RETRY_MAX_S
        if (any(isinstance(v, bool) or not math.isfinite(v) for v in (interval, age, base, maximum))
                or not 1 <= interval <= 86400 or not 0 <= age <= 2592000
                or not 1 <= base <= maximum <= 86400):
            raise ValueError("invalid Mem0 recovery timing settings")
        try:
            result = await recover_alpha_batches_once(
                add_memories=add_memories, allowed_user_ids=allowed_user_ids, cursor=cursor,
                max_age_seconds=age, max_files=settings.MEM0_BATCH_RECOVERY_SCAN_FILES,
                max_batches=settings.MEM0_BATCH_RECOVERY_MAX_BATCHES,
                retry_base_seconds=base, retry_max_seconds=maximum,
            )
            cursor = result["cursor"]
            if result.get("selected") or result.get("invalid_files"):
                log.info("Mem0 recovery: selected=%s completed=%s invalid_files=%s backoff=%s",
                         result.get("selected", 0), result["completed"],
                         result.get("invalid_files", 0), result.get("backoff_batches", 0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Mem0 recovery tick failed: %s", type(exc).__name__)
        try:
            await _recover_alpha_lineage_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Mem0 lineage recovery failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)


async def _recover_alpha_lineage_once():
    """Resume authorized durable repairs, without initializing an idle provider."""
    from llm_mem0 import lineage
    from llm_mem0.client import _get_mem0
    def repair():
        if not lineage.has_pending_repairs():
            return {"completed": 0, "failed": 0, "pending": 0}
        mem = _get_mem0()
        if mem is None:
            raise RuntimeError("memory store unavailable for lineage repair")
        return lineage.repair_dependents_sync(mem, limit=100)
    # This operation can commit metadata. Drain its actual thread before the
    # owning task ends, as with batch ACK/retry filesystem operations.
    result = await _batch_io(repair)
    if result.get("completed") or result.get("failed") or result.get("pending"):
        log.info("Mem0 lineage recovery: completed=%s failed=%s pending=%s",
                 result.get("completed", 0), result.get("failed", 0), result.get("pending", 0))
    return result

"""Long-term turn recording policy and dispatch."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable


log = logging.getLogger(__name__)


_last_mem0_record_at: dict[int, float] = {}
_MEM0_FORCE_RECORD_RE = re.compile(
    r"(覚えて|覚えといて|記録して|メモして|残しといて|残して|"
    r"remember this|please remember|don't forget|save this)",
    re.IGNORECASE,
)


def is_force_record_request(user_text: str) -> bool:
    return bool(_MEM0_FORCE_RECORD_RE.search(user_text or ""))


def should_auto_record_mem0(
    channel_id: int,
    user_text: str,
    response_text: str = "",
    *,
    enabled: bool,
    min_chars: int,
    interval_s: float,
) -> bool:
    """Return whether a turn passes the automatic recording throttle."""
    if not enabled:
        return False
    if is_force_record_request(user_text):
        return True
    effective_len = max(
        len((user_text or "").strip()),
        len((response_text or "").strip()),
    )
    if effective_len < min_chars:
        return False
    last = _last_mem0_record_at.get(channel_id, 0.0)
    return (time.time() - last) >= interval_s


def mark_mem0_recorded(channel_id: int) -> None:
    _last_mem0_record_at[channel_id] = time.time()


async def index_channel_message(
    message: Any,
    *,
    cap_text: Callable[[str, int], str],
    max_user_message_chars: int,
    append_jsonl: Callable[..., Any],
) -> None:
    """Persist an unaddressed channel message for later literal recall."""
    try:
        content = (message.content or "").strip()
        if len(content) < 2:
            return
        content = cap_text(content, max_user_message_chars)
        author = getattr(message.author, "display_name", "") or str(message.author)
        await asyncio.to_thread(
            append_jsonl,
            message.channel.id,
            {"role": "channel", "content": content, "author": author},
        )
    except Exception as exc:
        log.debug("channel-message index failed (non-fatal): %s", exc)


async def record_admin_turn(
    *,
    caller_is_admin: bool,
    message: Any,
    raw_user_text: str,
    response_text: str,
    mem0_user_id: str,
    conv_context: str,
    record_turn: Callable[..., Any],
    add_memories: Callable[..., Any],
    track_task: Callable[..., Any],
    load_history_channel: Callable[..., Any],
    is_force_record_request_hook: Callable[[str], bool] | None = None,
    request_id: str | None = None,
) -> None:
    """Dispatch the existing recorder for administrators only."""
    if not caller_is_admin:
        return
    await record_turn(
        message=message,
        raw_user_text=raw_user_text,
        response_text=response_text,
        mem0_user_id=mem0_user_id,
        conv_context=conv_context,
        add_memories=add_memories,
        track_task=track_task,
        load_history_channel=load_history_channel,
        is_force_record_request=(
            is_force_record_request_hook or is_force_record_request
        ),
        request_id=request_id,
    )

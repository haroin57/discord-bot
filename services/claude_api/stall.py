"""Stream stall watchdog for the direct-API path.

Polls a heartbeat every 10 s. After ``stall_threshold_s`` without a heartbeat
it fires ``status_callback`` once ("⏳ delayed"); after ``idle_timeout_s``
it cancels the parent task ("stream idle").

Differs from services/claude_sdk.py:547-566 in three ways:

1. **Async context manager** rather than an inline coroutine — keeps the
   lifecycle inside ``async with`` for guaranteed cleanup.
2. **Calls ``status_callback`` on stall.** The SDK-era watchdog only logged.
3. **Accepts ``cancel_event: asyncio.Event``** as a placeholder for the
   manual-cancel feature planned in keen-strolling-thunder.md. Currently
   never set; auto-cancel at ``idle_timeout_s`` is preserved.
"""

import asyncio
import logging
import time

from config import STREAM_IDLE_TIMEOUT_S, STREAM_STALL_THRESHOLD_S

from services.claude_api.status import _safe_status

log = logging.getLogger(__name__)


class StallWatchdog:
    def __init__(
        self,
        channel_id: int,
        status_callback=None,
        *,
        stall_threshold_s: float = STREAM_STALL_THRESHOLD_S,
        idle_timeout_s: float = STREAM_IDLE_TIMEOUT_S,
        cancel_event: asyncio.Event | None = None,
        poll_interval_s: float = 10.0,
    ):
        self.channel_id = channel_id
        self.status_callback = status_callback
        self.stall_threshold_s = stall_threshold_s
        self.idle_timeout_s = idle_timeout_s
        self.cancel_event = cancel_event
        self.poll_interval_s = poll_interval_s
        self._last = time.monotonic()
        self._warned = False
        self._task: asyncio.Task | None = None
        self._parent: asyncio.Task | None = None
        self._stream_idle = False
        # Diagnostics surfaced to entry.py's CancelledError handler so the
        # user-visible status_msg can be edited into a structured
        # "⏰ タイムアウトしました — rid=... idle=... 最後のturn=..." line
        # instead of being deleted and replaced with a context-free notice.
        self.idle_seconds: float = 0.0
        self.last_turn: int = -1
        self.cancel_reason: str = ""

    def heartbeat(self) -> None:
        self._last = time.monotonic()
        self._warned = False

    def set_turn(self, turn: int) -> None:
        """Record the agent-loop turn we are currently executing.

        Called by tool_loop at the top of each ``for turn in range(...)``
        iteration. Surfaces as ``last_turn`` on cancel so the user-facing
        diagnostic can say "最後のturn=N" (the deepest turn we entered).
        """
        if turn > self.last_turn:
            self.last_turn = turn

    @property
    def stream_idle(self) -> bool:
        return self._stream_idle

    async def __aenter__(self) -> "StallWatchdog":
        self._parent = asyncio.current_task()
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        return False

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_s)
            idle = time.monotonic() - self._last

            # Manual-cancel (currently a no-op since cancel_event is never set
            # in this PR; wired for keen-strolling-thunder.md).
            if self.cancel_event is not None and self.cancel_event.is_set():
                log.info("[ch=%d] cancel_event set, cancelling", self.channel_id)
                self.idle_seconds = idle
                self.cancel_reason = "user_cancel"
                self._stream_idle = True
                if self._parent is not None:
                    self._parent.cancel("user cancel")
                return

            if idle > self.idle_timeout_s:
                log.warning(
                    "[ch=%d] stream idle for %.0fs, cancelling",
                    self.channel_id, idle,
                )
                await _safe_status(
                    self.status_callback,
                    f"⏰ ストリームが {int(idle)} 秒応答なし、中断します",
                )
                self.idle_seconds = idle
                self.cancel_reason = "stream_idle"
                self._stream_idle = True
                if self._parent is not None:
                    self._parent.cancel("stream idle")
                return

            if idle > self.stall_threshold_s:
                # Log once on first stall to avoid spam; reset on heartbeat().
                # User-facing status edit re-fires every poll so the displayed
                # idle seconds advance instead of latching at the first value
                # (was the rid=8e6df64c UX bug: user saw "31秒" frozen for
                # 571s while real idle climbed to 602s).
                # bot.py _update_status() rate-limits to STATUS_INTERVAL_S=2.0s
                # with latest-wins, so a 10s poll cadence is safely throttled.
                if not self._warned:
                    log.warning(
                        "[ch=%d] stream stall: no message for %.0fs",
                        self.channel_id, idle,
                    )
                    self._warned = True
                await _safe_status(
                    self.status_callback,
                    f"⏳ 応答が遅延しています ({int(idle)}秒)",
                )

"""Tests for services.claude_api.stall.StallWatchdog."""

import asyncio

import pytest

from services.claude_api.stall import StallWatchdog


@pytest.mark.asyncio
async def test_no_stall_no_callback():
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async with StallWatchdog(
        channel_id=1, status_callback=cb,
        stall_threshold_s=1.0, idle_timeout_s=10.0, poll_interval_s=0.05,
    ) as wd:
        # Heartbeat frequently; no stall should fire.
        for _ in range(8):
            wd.heartbeat()
            await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_stall_threshold_invokes_callback():
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async with StallWatchdog(
        channel_id=1, status_callback=cb,
        stall_threshold_s=0.1, idle_timeout_s=10.0, poll_interval_s=0.05,
    ):
        await asyncio.sleep(0.5)
    # Expect at least one "⏳" callback, but not the cancel one.
    assert any("⏳" in c for c in calls)
    assert all("⏰" not in c for c in calls)


@pytest.mark.asyncio
async def test_stall_logs_once_but_status_advances_each_poll(caplog):
    # 2026-05-30 FIX A: the watchdog now re-emits the user-facing status
    # edit on EVERY poll while stalled so the displayed idle seconds advance
    # (was the rid=8e6df64c UX bug: "31秒" frozen for ~571s while real idle
    # climbed to 602s). The log.warning still fires only once per stall
    # episode to avoid spam.
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    import logging
    with caplog.at_level(logging.WARNING, logger="services.claude_api.stall"):
        async with StallWatchdog(
            channel_id=1, status_callback=cb,
            stall_threshold_s=0.05, idle_timeout_s=10.0, poll_interval_s=0.02,
        ):
            await asyncio.sleep(0.3)
    # status_callback should fire multiple times (one per poll while stalled).
    warn_callbacks = sum(1 for c in calls if "⏳" in c)
    assert warn_callbacks > 1, (
        f"expected multiple status edits while stalled, got {warn_callbacks}"
    )
    # But log.warning("stream stall: ...") should appear only ONCE per
    # stall period (heartbeat() resets _warned for the next stall episode).
    stall_log_lines = [
        r for r in caplog.records
        if "stream stall: no message" in r.getMessage()
    ]
    assert len(stall_log_lines) == 1, (
        f"expected exactly 1 'stream stall' log line, got {len(stall_log_lines)}"
    )


@pytest.mark.asyncio
async def test_idle_timeout_cancels_parent():
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async def main():
        async with StallWatchdog(
            channel_id=1, status_callback=cb,
            stall_threshold_s=10.0, idle_timeout_s=0.1, poll_interval_s=0.02,
        ) as wd:
            try:
                await asyncio.sleep(1.0)  # well past idle_timeout
            except asyncio.CancelledError:
                return wd.stream_idle

    result = await main()
    assert result is True  # stream_idle flagged before cancel
    assert any("⏰" in c for c in calls)


@pytest.mark.asyncio
async def test_cancel_event_triggers_cancel():
    calls: list[str] = []
    cancel_event = asyncio.Event()

    async def cb(msg):
        calls.append(msg)

    async def main():
        async with StallWatchdog(
            channel_id=1, status_callback=cb,
            stall_threshold_s=10.0, idle_timeout_s=10.0,
            cancel_event=cancel_event, poll_interval_s=0.02,
        ) as wd:
            await asyncio.sleep(0.05)
            cancel_event.set()
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return wd.stream_idle

    result = await main()
    assert result is True


@pytest.mark.asyncio
async def test_heartbeat_resets_warned_flag():
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async with StallWatchdog(
        channel_id=1, status_callback=cb,
        stall_threshold_s=0.05, idle_timeout_s=10.0, poll_interval_s=0.02,
    ) as wd:
        await asyncio.sleep(0.2)  # one warn
        wd.heartbeat()
        await asyncio.sleep(0.2)  # second warn after reset
    warns = sum(1 for c in calls if "⏳" in c)
    assert warns >= 2

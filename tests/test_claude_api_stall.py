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
async def test_stall_only_warns_once_per_idle_period():
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async with StallWatchdog(
        channel_id=1, status_callback=cb,
        stall_threshold_s=0.05, idle_timeout_s=10.0, poll_interval_s=0.02,
    ):
        await asyncio.sleep(0.3)
    # Same idle period — should warn once, not many times.
    warn_count = sum(1 for c in calls if "⏳" in c)
    assert warn_count == 1


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

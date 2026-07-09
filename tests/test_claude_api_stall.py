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
async def test_stall_threshold_emits_status_once(monkeypatch):
    monkeypatch.setattr(
        "services.claude_api.stall.STREAM_STALL_STATUS_ENABLED", True,
    )
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async with StallWatchdog(
        channel_id=1, status_callback=cb,
        stall_threshold_s=0.1, idle_timeout_s=10.0, poll_interval_s=0.05,
    ) as wd:
        await asyncio.sleep(0.5)
        assert wd._warned is True
    # Stall now surfaces ONE user-facing "waiting on API" status (deduped via
    # _warned), distinguishing a genuine no-event stall from "💭 thinking".
    assert sum("⏳" in c for c in calls) == 1
    assert all("⏰" not in c for c in calls)


@pytest.mark.asyncio
async def test_stall_status_deduped_per_idle_period(monkeypatch):
    monkeypatch.setattr(
        "services.claude_api.stall.STREAM_STALL_STATUS_ENABLED", True,
    )
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async with StallWatchdog(
        channel_id=1, status_callback=cb,
        stall_threshold_s=0.05, idle_timeout_s=10.0, poll_interval_s=0.02,
    ) as wd:
        await asyncio.sleep(0.3)
        assert wd._warned is True
    # Deduped via _warned — no per-poll spam.
    assert sum("⏳" in c for c in calls) == 1


@pytest.mark.asyncio
async def test_stall_status_disabled_no_callback(monkeypatch):
    monkeypatch.setattr(
        "services.claude_api.stall.STREAM_STALL_STATUS_ENABLED", False,
    )
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async with StallWatchdog(
        channel_id=1, status_callback=cb,
        stall_threshold_s=0.1, idle_timeout_s=10.0, poll_interval_s=0.05,
    ) as wd:
        await asyncio.sleep(0.4)
        assert wd._warned is True  # flag still flips (log-only path)
    assert all("⏳" not in c for c in calls)


@pytest.mark.asyncio
async def test_stall_status_reemits_after_heartbeat(monkeypatch):
    monkeypatch.setattr(
        "services.claude_api.stall.STREAM_STALL_STATUS_ENABLED", True,
    )
    calls: list[str] = []

    async def cb(msg):
        calls.append(msg)

    async with StallWatchdog(
        channel_id=1, status_callback=cb,
        stall_threshold_s=0.05, idle_timeout_s=10.0, poll_interval_s=0.02,
    ) as wd:
        await asyncio.sleep(0.2)
        assert sum("⏳" in c for c in calls) == 1
        wd.heartbeat()  # a stream event arrived — resets the dedup
        await asyncio.sleep(0.2)
    # After the heartbeat reset, a fresh stall surfaces the status again.
    assert sum("⏳" in c for c in calls) == 2


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
    async with StallWatchdog(
        channel_id=1, status_callback=None,
        stall_threshold_s=0.05, idle_timeout_s=10.0, poll_interval_s=0.02,
    ) as wd:
        await asyncio.sleep(0.2)
        assert wd._warned is True    # first stall detected (log-only)
        wd.heartbeat()
        assert wd._warned is False   # heartbeat clears the warned flag
        await asyncio.sleep(0.2)
        assert wd._warned is True    # warns again after the reset

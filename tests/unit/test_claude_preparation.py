"""Concurrent Claude preparation preserves isolation and cancellation."""

import asyncio
import json

import pytest

from services.claude_api import entry


async def test_selection_and_history_read_overlap(monkeypatch):
    selection_started = asyncio.Event()
    replay_started = asyncio.Event()
    release = asyncio.Event()
    expected = ([{"role": "user", "content": "synthetic history"}], "snapshot", "time")

    async def select(text, *, user_id):
        assert (text, user_id) == ("request", 20)
        selection_started.set()
        await release.wait()
        return {"docs": {"command": "test"}}

    async def read_in_thread(func, channel_id):
        assert func is entry.load_api_replay_channel
        assert channel_id == 10
        replay_started.set()
        await release.wait()
        return expected

    monkeypatch.setattr(entry, "select_mcp_servers", select)
    monkeypatch.setattr(entry.asyncio, "to_thread", read_in_thread)
    task = asyncio.create_task(entry._prepare_mcp_and_replay("request", 10, 20))
    try:
        await asyncio.wait_for(
            asyncio.gather(selection_started.wait(), replay_started.wait()), 1,
        )
    finally:
        release.set()
    selected, replay = await task
    assert selected == {"docs": {"command": "test"}}
    assert replay is expected


async def test_cancellation_collects_both_preparation_tasks(monkeypatch):
    started = [asyncio.Event(), asyncio.Event()]
    exited = [asyncio.Event(), asyncio.Event()]

    async def block(index):
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            exited[index].set()

    async def select(*args, **kwargs):
        return await block(0)

    async def read_in_thread(*args):
        return await block(1)

    monkeypatch.setattr(entry, "select_mcp_servers", select)
    monkeypatch.setattr(entry.asyncio, "to_thread", read_in_thread)
    task = asyncio.create_task(entry._prepare_mcp_and_replay("request", 10, 20))
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(event.is_set() for event in exited)


async def test_selector_failure_cancels_pending_history_read(monkeypatch):
    replay_started = asyncio.Event()
    replay_exited = asyncio.Event()
    failure = RuntimeError("selection failed")

    async def select(*args, **kwargs):
        await replay_started.wait()
        raise failure

    async def read_in_thread(*args):
        replay_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            replay_exited.set()

    monkeypatch.setattr(entry, "select_mcp_servers", select)
    monkeypatch.setattr(entry.asyncio, "to_thread", read_in_thread)
    with pytest.raises(RuntimeError) as caught:
        await asyncio.wait_for(entry._prepare_mcp_and_replay("request", 10, 20), 1)
    assert caught.value is failure
    assert replay_exited.is_set()


async def test_invalid_history_keeps_selected_tools(monkeypatch):
    async def select(*args, **kwargs):
        return {"docs": {}}

    async def read_in_thread(*args):
        raise json.JSONDecodeError("invalid", "", 0)

    monkeypatch.setattr(entry, "select_mcp_servers", select)
    monkeypatch.setattr(entry.asyncio, "to_thread", read_in_thread)
    assert await entry._prepare_mcp_and_replay("request", 10, 20) == (
        {"docs": {}}, None,
    )


async def test_concurrent_channels_keep_their_own_preparation(monkeypatch):
    async def select(text, *, user_id):
        await asyncio.sleep(0)
        return {text: {"user_id": user_id}}

    async def read_in_thread(func, channel_id):
        await asyncio.sleep(0)
        return ([{"role": "user", "content": str(channel_id)}], "", "")

    monkeypatch.setattr(entry, "select_mcp_servers", select)
    monkeypatch.setattr(entry.asyncio, "to_thread", read_in_thread)
    results = await asyncio.gather(
        entry._prepare_mcp_and_replay("one", 10, 1),
        entry._prepare_mcp_and_replay("two", 20, 2),
    )
    assert results == [
        ({"one": {"user_id": 1}}, ([{"role": "user", "content": "10"}], "", "")),
        ({"two": {"user_id": 2}}, ([{"role": "user", "content": "20"}], "", "")),
    ]

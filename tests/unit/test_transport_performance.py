"""Resource lifecycle and concurrency regressions; no provider/network calls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.claude_api._cli_runner import AvailabilityCache
from services.codex_app_server import manager as manager_module
from services.codex_app_server import session as session_module
from tests.test_codex_app_server_manager import _FakeSession
from tests.test_codex_app_server_session import _ScriptedClient
from tests.unit.test_codex_parity import complete_events


@pytest.mark.asyncio
@pytest.mark.parametrize('available', [True, False])
async def test_cli_availability_collapses_concurrent_probes(available):
    cache = AvailabilityCache()
    calls = 0

    async def probe():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return available

    assert await asyncio.gather(*(cache.get(probe) for _ in range(20))) == [available] * 20
    assert calls == 1
    cache.reset()
    assert await cache.get(probe) is available
    assert calls == 2


@pytest.mark.asyncio
async def test_cli_cancelled_probe_does_not_block_waiters():
    cache = AvailabilityCache()
    started = asyncio.Event()

    async def pending():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(cache.get(pending))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await cache.get(AsyncMock(return_value=True)) is True


@pytest.mark.asyncio
async def test_codex_non_streaming_helper_does_not_materialize_partial_callbacks(monkeypatch):
    calls = []
    original = session_module._invoke

    async def record(callback, *args):
        calls.append(callback)
        return await original(callback, *args)

    monkeypatch.setattr(session_module, '_invoke', record)
    events = []
    for _ in range(200):
        events.extend([
            {'method': 'item/agentMessage/delta', 'params': {'itemId': 'msg', 'delta': 'x' * 100}},
            {'method': 'item/reasoning/summaryTextDelta', 'params': {'delta': 'y' * 100}},
        ])
    client = _ScriptedClient(events=events + complete_events('answer'))
    session = session_module.CodexSession(cwd='/workspace', codex_bin='/opt/codex', client_factory=lambda **_: client)
    result = await session.run_turn('prompt', model='gpt-5.6-sol', effort='high', developer_instructions='test')
    assert result.final_text == 'answer'
    # The final thinking flush is still required even without a consumer.
    assert len(calls) < 5


def make_manager(tmp_path, monkeypatch, **kwargs):
    _FakeSession.instances.clear()
    monkeypatch.setattr(manager_module, 'CodexSession', _FakeSession)
    monkeypatch.setattr(manager_module, 'build_app_server_extra_args', lambda *args: [])
    return manager_module.CodexSessionManager(state_path=str(tmp_path / 'state.json'), **kwargs)


async def turn(manager, channel=1, user=2):
    return await manager.run_turn(channel, user, 'prompt', model='gpt-5.6-sol',
                                  effort='high', developer_instructions='test')


@pytest.mark.asyncio
async def test_idle_sessions_retire_without_new_traffic_and_resume_thread(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, idle_timeout_s=0.02)
    first = await turn(manager)
    previous = _FakeSession.instances[0]
    await asyncio.sleep(0.06)
    assert previous.closed
    assert not manager.has_live_session(1, 2)
    assert manager.has_thread(1, 2)
    resumed = await turn(manager)
    assert resumed.thread_id == first.thread_id
    assert _FakeSession.instances[1].kwargs['stored_thread_id'] == first.thread_id
    await manager.close_all()


@pytest.mark.asyncio
async def test_idle_timer_never_retires_active_or_waiting_channel(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, idle_timeout_s=0.02)
    await turn(manager)
    session = _FakeSession.instances[0]
    async with manager._channel_locks[1]:
        queued = asyncio.create_task(turn(manager))
        await asyncio.sleep(0.06)
        assert not session.closed
    await asyncio.wait_for(queued, 1)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = session.run_turn

    async def long_turn(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    session.run_turn = long_turn
    active = asyncio.create_task(turn(manager))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0.06)
    assert not session.closed
    assert manager._active_keys == {'1:2'}
    release.set()
    await active
    await manager.close_all()


@pytest.mark.asyncio
async def test_idle_timer_reset_and_disabled_setting(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, idle_timeout_s=0)
    await turn(manager)
    assert not manager._idle_handles
    await manager.close_all()


@pytest.mark.asyncio
async def test_manager_shutdown_closes_independent_sessions_concurrently(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, idle_timeout_s=100)
    for channel in range(8):
        await turn(manager, channel)
    active = peak = 0

    async def close():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    for session in manager._sessions.values():
        session.close = close
    await manager.close_all()
    assert peak == 8
    assert not manager._idle_handles


@pytest.mark.asyncio
async def test_bridge_different_servers_overlap_but_same_server_stays_affine(monkeypatch):
    from mcp_codex_bridge import ServerActors
    from services.claude_api import mcp_client

    active = peak = 0
    owners = {}
    closed = {}
    sequence = []

    def pool(*, stack, available_configs):
        key = next(iter(available_configs))

        async def close():
            closed[key] = asyncio.current_task()

        stack.push_async_callback(close)

        async def spawn(name):
            owners.setdefault(key, asyncio.current_task())
            assert owners[key] is asyncio.current_task()
            return SimpleNamespace(tools=[])

        async def call(name, arguments):
            nonlocal active, peak
            assert owners[key] is asyncio.current_task()
            active += 1
            peak = max(peak, active)
            sequence.append((key, arguments['index']))
            await asyncio.sleep(0.015)
            active -= 1
            return str(arguments['index'])

        return SimpleNamespace(ensure_spawned=spawn, call_tool=call)

    monkeypatch.setattr(mcp_client, 'McpPool', pool)
    actors = ServerActors({'one': {}, 'two': {}})
    try:
        results = await asyncio.gather(*(
            actors.submit('call', f'mcp__{key}__test', {'index': index})
            for key, index in [('one', 1), ('one', 2), ('two', 3)]
        ))
        assert results == ['1', '2', '3']
        assert peak == 2
        assert sequence.index(('one', 1)) < sequence.index(('one', 2))
    finally:
        await actors.close()
    assert owners == closed
    assert owners['one'] is not owners['two']
    with pytest.raises(RuntimeError, match='closed'):
        await actors.submit('discover', 'one', {})


@pytest.mark.asyncio
async def test_bridge_denies_unknown_servers_before_allocating_actor():
    from mcp_codex_bridge import ServerActors
    actors = ServerActors({'one': {}})
    with pytest.raises(ValueError, match='authorized'):
        await actors.submit('discover', 'two', {})
    assert not actors.actors
    await actors.close()


@pytest.mark.asyncio
async def test_dead_bridge_actor_fails_queued_request_and_next_submit_restarts(monkeypatch):
    import mcp_codex_bridge as bridge
    attempts = 0

    async def actor(queue, configs, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('startup failed')
        try:
            while True:
                _, _, _, future = await queue.get()
                future.set_result('restarted')
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(bridge, 'run_actor', actor)
    actors = bridge.ServerActors({'one': {}})
    try:
        with pytest.raises(RuntimeError, match='actor stopped'):
            await asyncio.wait_for(actors.submit('discover', 'one', {}), 1)
        assert await asyncio.wait_for(actors.submit('discover', 'one', {}), 1) == 'restarted'
        assert attempts == 2
    finally:
        await actors.close()


@pytest.mark.asyncio
async def test_bridge_close_before_actor_first_runs_cancels_every_waiter(monkeypatch):
    import mcp_codex_bridge as bridge

    async def actor(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(bridge, 'run_actor', actor)
    actors = bridge.ServerActors({'one': {}})
    requests = [asyncio.create_task(actors.submit('discover', 'one', {})) for _ in range(4)]
    await asyncio.sleep(0)
    await actors.close()
    results = await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), 1)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)


@pytest.mark.asyncio
async def test_manager_shutdown_rejects_new_and_queued_acquires(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, idle_timeout_s=0.01)
    await turn(manager)
    async with manager._channel_locks[1]:
        queued = asyncio.create_task(turn(manager))
        await asyncio.sleep(0)
        await manager.close_all()
    with pytest.raises(RuntimeError, match='closed'):
        await queued
    with pytest.raises(RuntimeError, match='closed'):
        await turn(manager, 3)
    assert len(_FakeSession.instances) == 1
    assert not manager._idle_handles


@pytest.mark.asyncio
async def test_clear_cancels_idle_timer_and_never_revives_thread(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, idle_timeout_s=0.01)
    await turn(manager)
    assert await manager.clear_thread(1, 2)
    await asyncio.sleep(0.03)
    assert not manager.has_thread(1, 2)
    assert not manager._idle_handles
    assert not manager._sessions
    await manager.close_all()


@pytest.mark.asyncio
async def test_cancelled_queued_turn_rearms_idle_cleanup(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, idle_timeout_s=0.01)
    await turn(manager)
    previous = _FakeSession.instances[0]
    async with manager._channel_locks[1]:
        queued = asyncio.create_task(turn(manager))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
    await asyncio.sleep(0.04)
    assert previous.closed
    assert manager.has_thread(1, 2)
    await manager.close_all()

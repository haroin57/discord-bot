"""Research MCP scopes stay in their actor despite parallel callers."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest

from services.deep_research import mcp_actor


@pytest.fixture
def fake_pool(monkeypatch):
    events = []
    ready = {}

    @asynccontextmanager
    async def scope(name):
        owner = asyncio.current_task()
        events.append(('enter', name, owner))
        async with anyio.create_task_group():
            try:
                yield
            finally:
                assert asyncio.current_task() is owner
                events.append(('exit', name, owner))

    class Pool:
        def __init__(self, *, stack, available_configs, process_observer):
            self.stack = stack
            self.name = next(iter(available_configs))
            self.started = False

        async def ensure_spawned(self, name):
            if not self.started:
                await self.stack.enter_async_context(scope(name))
                self.started = True
            return SimpleNamespace(tools=[])

        async def call_tool(self, name, args):
            events.append(('call', self.name, asyncio.current_task()))
            if args.get('wait'):
                ready.setdefault(self.name, asyncio.Event()).set()
                await asyncio.Event().wait()
            return f'result {self.name}'

    monkeypatch.setattr(mcp_actor, 'McpPool', Pool)
    return events, ready


@pytest.mark.asyncio
async def test_child_researchers_share_actor_and_teardown_owner(fake_pool):
    events, _ = fake_pool
    async with AsyncExitStack() as stack:
        pool = mcp_actor.ResearchMcpPool(stack=stack, available_configs={'a': {}}, timeout_s=1)
        first, second = await asyncio.gather(
            pool.call_tool('mcp__a__search', {}), pool.call_tool('mcp__a__search', {}))
        assert first == second == 'result a'
        assert 'a' in pool.handles
    assert [kind for kind, _, _ in events] == ['enter', 'call', 'call', 'exit']
    assert len({task for _, _, task in events}) == 1


@pytest.mark.asyncio
async def test_servers_remain_parallel_and_abort_cleans_active_calls(fake_pool):
    events, ready = fake_pool
    async with AsyncExitStack() as stack:
        pool = mcp_actor.ResearchMcpPool(stack=stack, available_configs={'a': {}, 'b': {}}, timeout_s=5)
        waiting = asyncio.create_task(pool.call_tool('mcp__a__search', {'wait': True}))
        for _ in range(30):
            if 'a' in ready:
                break
            await asyncio.sleep(0.001)
        assert await asyncio.wait_for(pool.call_tool('mcp__b__search', {}), 0.2) == 'result b'
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert sorted(name for kind, name, _ in events if kind == 'exit') == ['a', 'b']


@pytest.mark.asyncio
async def test_timeout_keeps_actor_usable_and_closed_pool_fails(fake_pool):
    async with AsyncExitStack() as stack:
        pool = mcp_actor.ResearchMcpPool(stack=stack, available_configs={'a': {}}, timeout_s=0.01)
        with pytest.raises(TimeoutError):
            await pool.call_tool('mcp__a__search', {'wait': True})
        assert await pool.call_tool('mcp__a__search', {}) == 'result a'
        with pytest.raises(KeyError):
            await pool.call_tool('mcp__unauthorized__search', {})
    with pytest.raises(RuntimeError, match='closed'):
        await pool.ensure_spawned('a')


@pytest.mark.asyncio
async def test_eager_and_parent_cancellation_preserve_task_affinity(fake_pool):
    events, _ = fake_pool
    started = asyncio.Event()
    async def parent():
        async with AsyncExitStack() as stack:
            pool = mcp_actor.ResearchMcpPool(stack=stack, available_configs={'a': {}})
            await pool.spawn_eager(['a'])
            started.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(parent())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [kind for kind, _, _ in events] == ['enter', 'exit']


@pytest.mark.asyncio
async def test_shutdown_before_actor_first_step_resolves_queued_caller(fake_pool):
    async with AsyncExitStack() as stack:
        pool = mcp_actor.ResearchMcpPool(stack=stack, available_configs={'a': {}})
        caller = asyncio.create_task(pool.call_tool('mcp__a__search', {}))
        await asyncio.sleep(0)
        await pool.aclose()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 0.2)

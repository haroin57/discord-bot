"""Run with MEM0_ALPHA_ENABLED=1 and the candidate wheel/PYTHONPATH."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('MEM0_ALPHA_ENABLED') != '1', reason='alpha-only lane')


@pytest.mark.asyncio
async def test_alpha_morphology_is_off_loop(monkeypatch):
    from services.mem0 import search
    loop_thread = threading.get_ident()
    workers = []
    def tokenize(query):
        workers.append(threading.get_ident())
        return 'tokens'
    monkeypatch.setattr(search, '_expand_query_via_graph', AsyncMock(return_value=[]))
    monkeypatch.setattr(search, '_expand_query_via_morpho', tokenize)
    assert await search._expand_queries('query', 'user', rewrite=False) == ['query', 'tokens']
    assert workers[0] != loop_thread


@pytest.mark.asyncio
async def test_alpha_core_single_flight_preserves_scope_and_limits_users(monkeypatch):
    from services.mem0 import search
    from llm_mem0 import client
    monkeypatch.setattr(search, '_CORE_CACHE', {})
    monkeypatch.setattr(search, '_CORE_CACHE_USERS', 2)
    monkeypatch.setenv('MEM0_SCOPE_FILTER_ENABLED', 'true')
    monkeypatch.setenv('MEM0_CORE_MEMORY_ENABLED', 'true')
    async def fetch(uid):
        await asyncio.sleep(.01)
        return [{'id': 'fact', 'metadata': {'importance': 5, 'tier': 0, 'scope': 'conversation:20'}}]
    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(client, 'get_all_memories', fetch_mock)
    rows = await asyncio.gather(*(search.get_core_memories('user', current_channel_id=str(20 + i % 2))
                                 for i in range(12)))
    assert fetch_mock.await_count == 1
    assert [bool(row) for row in rows] == [i % 2 == 0 for i in range(12)]
    await search.get_core_memories('user2')
    await search.get_core_memories('user3')
    assert set(search._CORE_CACHE) == {'user2', 'user3'}


@pytest.mark.asyncio
async def test_alpha_graph_provider_reads_bounded_and_ordered(monkeypatch):
    from services.mem0 import search, graph, priming
    monkeypatch.setenv('MEM0_SCOPE_FILTER_ENABLED', 'true')
    monkeypatch.setenv('MEM0_HIDE_ARCHIVED', 'true')
    monkeypatch.setattr(graph, 'get_fact_entities', lambda fid: [NS(id=1)])
    monkeypatch.setattr(graph, 'get_co_occurring_entities', lambda *a, **k: [])
    monkeypatch.setattr(graph, 'get_related_facts', lambda *a, **k: [f'f{i}' for i in range(12)])
    monkeypatch.setattr(priming, 'get_primed_entities', lambda *a: set())
    monkeypatch.setattr(priming, 'touch', lambda *a: None)
    lock = threading.Lock()
    active = maximum = 0
    def get(fid):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(.005 * (1 + int(fid[1:]) % 3))
            if fid == 'f3':
                raise RuntimeError('fixture provider failure')
            metadata = {'scope': 'conversation:other'} if fid == 'f4' else {}
            if fid == 'f5':
                metadata['valid_to'] = 1
            return {'id': fid, 'memory': fid, 'metadata': metadata}
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(search, '_get_mem0', lambda: NS(get=get))
    initial = [{'id': 'seed', 'memory': 'seed', 'score': .1}]
    rows = await search._graph_expand_candidates(initial, 'user', current_channel_id='20')
    assert [r['id'] for r in rows] == ['seed', 'f0', 'f1', 'f2', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11']
    assert 1 < maximum <= 4
    assert all(r['_graph_expanded'] for r in rows[1:])


@pytest.mark.asyncio
async def test_alpha_cancelled_turn_drains_core_task(monkeypatch):
    from services.mem0 import search
    started, finished = asyncio.Event(), asyncio.Event()
    async def core(*a, **k):
        started.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()
    async def expand(*a, **k):
        await asyncio.Future()
    monkeypatch.setattr(search, 'get_core_memories', core)
    monkeypatch.setattr(search, '_expand_queries', expand)
    task = asyncio.create_task(search.search_memories_smart('query', 'user', include_core=True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_alpha_shared_janome_generator_is_serialized(monkeypatch):
    from services.mem0 import morpho
    active = maximum = 0
    def tokenize(text):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            time.sleep(.001)
            yield NS(part_of_speech='名詞,一般', surface='一致')
        finally:
            active -= 1
    monkeypatch.setattr(morpho, '_get_tokenizer', lambda: NS(tokenize=tokenize))
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(morpho.tokenize_keywords, ['fixture'] * 20))
    assert rows == [['一致']] * 20
    assert maximum == 1

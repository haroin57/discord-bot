"""Performance invariants use isolated stores and no Discord/LLM requests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import threading
import time
from unittest.mock import AsyncMock

import pytest

from services import guild_context as gc


def record(mid, *, gid=10, cid=20, created=None):
    return dict(message_id=mid, guild_id=gid, channel_id=cid, author_id=30,
                author='fixture', content=f'fixture-{mid}', bot=False,
                created_at=created or time.time())


def test_page_uses_one_connection_and_preserves_gateway_edits_deletes(tmp_path, monkeypatch):
    store = gc.GuildContextStore(tmp_path / 'guild.sqlite')
    store.edit(message_id=1, guild_id=10, channel_id=20, content='correction', version=time.time() + 10)
    store.delete([2])
    original = store._connect
    calls = []
    def connect():
        calls.append(1)
        return original()
    monkeypatch.setattr(store, '_connect', connect)
    store.record_many([record(mid) for mid in range(1, 61)])
    assert len(calls) == 1
    rows = {r['message_id']: r for r in store.recent(10)}
    assert len(rows) == 59
    assert rows[1]['content'] == 'correction'
    assert 2 not in rows


def test_batch_caps_each_guild_and_preserves_recent_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, '_MAX_ROWS_PER_GUILD', 3)
    store = gc.GuildContextStore(tmp_path / 'guild.sqlite')
    rows = [record(mid, gid=10 if mid < 5 else 11, created=time.time() - 10 + mid)
            for mid in range(1, 10)]
    rows[0]['version'] = time.time() + 10
    store.record_many(rows)
    assert [r['message_id'] for r in store.recent(10)] == [1, 4, 3]
    assert [r['message_id'] for r in store.recent(11)] == [9, 8, 7]


def test_updated_read_is_indexed_and_reconciliation_does_not_fetch_content(tmp_path):
    store = gc.GuildContextStore(tmp_path / 'guild.sqlite')
    store.record_many([record(1, created=time.time() - 60), record(2, cid=21)])
    db = store._connect()
    try:
        plan = ' '.join(row[3] for row in db.execute('''EXPLAIN QUERY PLAN
            SELECT * FROM messages WHERE guild_id=10 AND MAX(created_at,version)>0
            ORDER BY MAX(created_at,version) DESC, message_id DESC LIMIT 1000'''))
        assert 'messages_guild_updated' in plan
        assert 'TEMP B-TREE' not in plan
    finally:
        db.close()
    assert store.history_ids(10, 20, before=time.time() - 10) == {1}


def test_render_skips_impossible_large_rows_but_retains_older_small_rows(monkeypatch):
    rows = [dict(record(i), version=0) for i in range(1000, 0, -1)]
    for row in rows:
        row['content'] = 'x' * 6000
    rows[-1]['content'] = 'older short correction'
    real_dumps = gc.json.dumps
    calls = []
    def dumps(*a, **k):
        calls.append(1)
        return real_dumps(*a, **k)
    monkeypatch.setattr(gc.json, 'dumps', dumps)
    rendered = gc.render(rows, visible_channel_ids={20})
    assert 'older short correction' in rendered
    assert len(rendered) <= gc._MAX_CONTEXT_CHARS
    assert len(calls) < 10


@pytest.mark.asyncio
async def test_partial_history_is_batched_without_false_deletion(tmp_path, monkeypatch):
    monkeypatch.setenv('GUILD_CONTEXT_ENABLED', 'true')
    monkeypatch.setattr(gc, '_hydrated', {})
    monkeypatch.setattr(gc, '_hydrate_locks', {})
    monkeypatch.setattr(gc, '_store_lock', asyncio.Lock())
    store = gc.GuildContextStore(tmp_path / 'guild.sqlite')
    store.record(**record(100, created=time.time() - 60))
    monkeypatch.setattr(gc, 'get_store', lambda: store)
    guild = NS(id=10, text_channels=[], threads=[])
    channel = NS(id=20, guild=guild, permissions_for=lambda member: NS(view_channel=True, read_message_history=True))
    async def history(limit):
        yield NS(id=101, guild=guild, channel=channel, author=NS(id=30, display_name='fixture', bot=False),
                 content='partial receipt', created_at=datetime.now(timezone.utc), edited_at=None)
        raise RuntimeError('isolated fixture failure')
    channel.history = history
    guild.text_channels = [channel]
    await gc._hydrate_visible(NS(guild=guild, channel=channel, author=NS(id=30)))
    assert {r['message_id'] for r in store.recent(10)} == {100, 101}
    assert not gc._hydrated


@pytest.mark.asyncio
async def test_janome_expansion_does_not_run_on_event_loop(monkeypatch):
    from services.mem0 import search
    owner = threading.get_ident()
    observed = []
    def tokenize(query):
        observed.append(threading.get_ident())
        return 'same quality tokens'
    monkeypatch.setattr(search, '_expand_query_via_graph', AsyncMock(return_value=[]))
    monkeypatch.setattr(search, '_expand_query_via_morpho', tokenize)
    assert await search._expand_queries('fixture query', 'u', rewrite=False) == ['fixture query', 'same quality tokens']
    assert observed and observed[0] != owner


@pytest.mark.asyncio
async def test_canonical_filter_does_not_run_on_event_loop(monkeypatch):
    from services.mem0 import search, integrity
    owner = threading.get_ident()
    observed = []
    def visible(ids, user_id, **kwargs):
        observed.append(threading.get_ident())
        return {identifier: True for identifier in ids}
    monkeypatch.setattr(integrity, 'canonical_facts_visible', visible)
    monkeypatch.setattr(search, '_get_mem0', lambda: NS(search=lambda **kwargs: [{'id': 'f', 'score': .1}]))
    monkeypatch.setattr(search, 'ensure_valid_token', AsyncMock())
    monkeypatch.setattr(search, '_refresh_mem0_client_token', lambda: None)
    assert await search.search_memories('fixture', 'u') == [{'id': 'f', 'score': .1}]
    assert observed and observed[0] != owner


@pytest.mark.asyncio
async def test_core_cache_single_fill_preserves_per_channel_scope(monkeypatch):
    from services.mem0 import search, client
    import config
    monkeypatch.setattr(search, '_CORE_CACHE', {})
    monkeypatch.setattr(config, 'MEM0_SCOPE_FILTER_ENABLED', True)
    monkeypatch.setenv('MEM0_INTEGRITY_MODE', 'legacy')
    async def fetch(user_id):
        await asyncio.sleep(.01)
        return [{'id': 'f', 'metadata': {'importance': 5, 'tier': 0, 'scope': 'conversation:20'}}]
    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(client, 'get_all_memories', mock)
    results = await asyncio.gather(*(search.get_core_memories('u', current_channel_id=str(20 + i % 2))
                                     for i in range(12)))
    assert mock.await_count == 1
    assert all(bool(value) == (i % 2 == 0) for i, value in enumerate(results))


@pytest.mark.asyncio
async def test_core_cache_has_bounded_user_retention(monkeypatch):
    from services.mem0 import search, client
    monkeypatch.setattr(search, '_CORE_CACHE', {})
    monkeypatch.setattr(search, '_CORE_CACHE_USERS', 2)
    monkeypatch.setattr(client, 'get_all_memories', AsyncMock(return_value=[]))
    for uid in ('u1', 'u2', 'u3'):
        await search.get_core_memories(uid)
    assert set(search._CORE_CACHE) == {'u2', 'u3'}


def test_canonical_batch_matches_single_checks_and_uses_one_connection(tmp_path, monkeypatch):
    from services.mem0 import integrity
    repository = integrity.CanonicalRepository(tmp_path / 'canonical.sqlite')
    repository.create_fact('u', 'public', {}, fact_id='f1', legacy_id='p1', project_vector=False)
    repository.create_fact('u', 'scoped', {'scope': 'conversation:20'}, fact_id='f2', project_vector=False)
    repository.create_fact('other', 'other owner', {}, fact_id='f3', project_vector=False)
    repository.create_fact('u', 'archived', {}, fact_id='f4', project_vector=False)
    repository.archive_fact('f4', reason='fixture')
    monkeypatch.setattr(integrity, 'get_repository', lambda: repository)
    monkeypatch.setenv('MEM0_INTEGRITY_MODE', 'canonical')
    ids = ['f1', 'p1', 'f2', 'f3', 'f4', 'missing']
    expected = {fid: integrity.canonical_fact_visible(fid, 'u', current_channel_id='20') for fid in ids}
    original = repository._connect
    calls = []
    def connect():
        calls.append(1)
        return original()
    monkeypatch.setattr(repository, '_connect', connect)
    assert integrity.canonical_facts_visible(ids, 'u', current_channel_id='20') == expected
    assert len(calls) == 1
    assert not integrity.canonical_facts_visible(['f2'], 'u', current_channel_id='21')['f2']


def test_shared_janome_generator_is_serialized(monkeypatch):
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


@pytest.mark.asyncio
async def test_cancelled_recall_drains_parallel_core(monkeypatch):
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
    task = asyncio.create_task(search.search_memories_smart('fixture', 'u', include_core=True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()

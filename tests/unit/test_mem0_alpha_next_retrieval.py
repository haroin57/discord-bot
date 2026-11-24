"""Second-stage alpha retrieval contracts, with private state and fake providers."""
import asyncio
import copy
import os
import socket
import sqlite3
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(autouse=True)
def isolated_alpha(monkeypatch, tmp_path):
    from llm_mem0 import bm25_index as bm, graph, priming, search
    for module, name in ((bm, "facts.sqlite"), (graph, "graph.sqlite")):
        monkeypatch.setattr(module._store, "_path", str(tmp_path / name))
        monkeypatch.setattr(module._store, "_wal_ready_path", None)
    monkeypatch.setattr(search, "_CORE_CACHE", {})
    monkeypatch.setattr(priming, "_buffer", {})
    for key, value in {
        "LLM_MEM0_STATE_DIR": str(tmp_path), "MEM0_COLLECTION_NAME": "synthetic_next",
        "MEM0_HIDE_ARCHIVED": "1", "MEM0_SCOPE_FILTER_ENABLED": "1",
        "MEM0_HYBRID_ENABLED": "1", "MEM0_BM25_LIMIT": "20", "MEM0_CORE_MEMORY_ENABLED": "1",
        "MEM0_CORE_CACHE_TTL_S": "300", "MEM0_EPISODE_BUNDLE_ENABLED": "0",
        "MEM0_RECALL_REINFORCE_ENABLED": "0", "MEM0_FOK_ENABLED": "0",
        "MEM0_PRIMING_ACTIVATION_BONUS": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(search, "_get_mem0", lambda: None)
    monkeypatch.setattr(search, "complete_json", AsyncMock(return_value=None))
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden in synthetic retrieval tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def fact(fid, text="Orion", owner="user", **metadata):
    return {"id": fid, "memory": text, "user_id": owner, "score": .1, "metadata": metadata}


def provider(monkeypatch, rows, vector=()):
    from llm_mem0 import search
    by_id = {r["id"]: r for r in rows}
    gets = []
    def get(fid):
        gets.append(fid)
        return copy.deepcopy(by_id.get(fid))
    monkeypatch.setattr(search, "_get_mem0", lambda: NS(get=get))
    monkeypatch.setattr(search, "mem_search", lambda *a, **kw: copy.deepcopy(vector[:kw["limit"]]))
    return gets


@pytest.mark.parametrize("invalid", ["archived", "future", "scope", "owner", "deleted"])
@pytest.mark.asyncio
async def test_lexical_refill_after_authoritative_validation(monkeypatch, invalid):
    from llm_mem0 import bm25_index as bm, search
    changes = {"archived": {"valid_to": 1}, "future": {"valid_from": 253402300799},
               "scope": {"scope": "conversation:other"}, "owner": {"owner": "other"}, "deleted": {}}
    rejected = [fact(f"reject-{i}", **changes[invalid]) for i in range(20)]
    valid = fact("valid", "Orion " + "background " * 30, scope="conversation:channel")
    gets = provider(monkeypatch, ([valid] if invalid == "deleted" else rejected + [valid]))
    for row in rejected + [valid]:
        assert bm.index_fact(row["id"], row["memory"], "user")
    real = bm.search_bm25
    limits = []
    def lexical(query, user_id, limit):
        limits.append(limit)
        return real(query, user_id, limit)
    monkeypatch.setattr(bm, "search_bm25", lexical)
    result = await search.search_memories_hybrid("Orion", "user", 1, current_channel_id="channel")
    assert [r["id"] for r in result] == ["valid"]
    assert result[0]["bm25_rank"] == 21
    assert limits == [20, 40]
    assert len(gets) == len(set(gets)) == 21


@pytest.mark.asyncio
async def test_lexical_refill_budget_and_exhaustion(monkeypatch):
    from llm_mem0 import bm25_index as bm, search
    gets = provider(monkeypatch, [])
    limits = []
    def lexical(query, user_id, limit):
        limits.append(limit)
        return [{"id": str(i), "rank": i + 1} for i in range(limit)]
    monkeypatch.setattr(bm, "search_bm25", lexical)
    assert await search.search_memories_hybrid("Orion", "user", 1) == []
    assert limits == [20, 40, 80]
    assert len(gets) == len(set(gets)) == 80
    limits.clear()
    monkeypatch.setenv("MEM0_BM25_LIMIT", "100")
    assert await search.search_memories_hybrid("Orion", "user", 1) == []
    assert limits == [100, 200]


@pytest.mark.asyncio
async def test_failed_refill_keeps_vector_result(monkeypatch):
    from llm_mem0 import bm25_index as bm, search
    provider(monkeypatch, [fact("vector")], vector=[fact("vector")])
    def lexical(query, user_id, limit):
        if limit > 20:
            raise RuntimeError("synthetic index failure")
        return [{"id": str(i), "rank": i + 1} for i in range(limit)]
    monkeypatch.setattr(bm, "search_bm25", lexical)
    assert [r["id"] for r in await search.search_memories_hybrid("Orion", "user", 1)] == ["vector"]


@pytest.mark.asyncio
async def test_lexical_refill_preserves_as_of_and_cancellation(monkeypatch):
    from llm_mem0 import bm25_index as bm, search
    rows = [fact(str(i), valid_from=200) for i in range(20)] + [fact("past", valid_to=200)]
    gets = provider(monkeypatch, rows)
    monkeypatch.setattr(bm, "search_bm25", lambda q, u, limit: [
        {"id": row["id"], "rank": i + 1} for i, row in enumerate(rows[:limit])])
    assert [r["id"] for r in await search.search_memories_at_time("Orion", "user", 150, 1)] == ["past"]
    assert len(gets) == 21 and search._AS_OF.get() is None
    started = asyncio.Event()
    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(search, "search_memories", blocked)
    task = asyncio.create_task(search.search_memories_hybrid("Orion", "user"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("hops", [0, 1, 2, 3])
@pytest.mark.asyncio
async def test_graph_hops_count_edges_and_stop_at_boundary(monkeypatch, hops):
    from llm_mem0 import graph, search
    monkeypatch.setenv("MEM0_GRAPH_SPREAD_MAX_HOPS", str(hops))
    monkeypatch.setenv("MEM0_GRAPH_SPREAD_MIN_ACTIVATION", "0")
    entities = [graph.upsert_entity(f"Entity {i}", "topic", "user") for i in range(4)]
    for i, eid in enumerate(entities):
        graph.add_edge(f"fact-{i}", eid)
    for i in range(3):
        graph.add_edge(f"bridge-{i}", entities[i])
        graph.add_edge(f"bridge-{i}", entities[i + 1])
    rows = [fact(f"fact-{i}") for i in range(4)]
    provider(monkeypatch, rows)
    result = await search._graph_expand_candidates(rows[:1], "user")
    assert {r["id"] for r in result} == {f"fact-{i}" for i in range(hops + 1)}


@pytest.mark.asyncio
async def test_graph_cycle_owner_scope_and_min_activation(monkeypatch):
    from llm_mem0 import graph, search
    monkeypatch.setenv("MEM0_GRAPH_SPREAD_MAX_HOPS", "3")
    monkeypatch.setenv("MEM0_GRAPH_SPREAD_MIN_ACTIVATION", "0.05")
    monkeypatch.setattr(graph, "get_fact_entities", lambda fid: [NS(id=1, user_id="user"), NS(id=9, user_id="other")])
    calls = []
    def neighbors(eid, **kw):
        calls.append(eid)
        assert kw["limit"] == 5
        return {1: [(2, .8), (3, .001)], 2: [(1, .8)]}.get(eid, [])
    monkeypatch.setattr(graph, "get_co_occurring_entities", neighbors)
    monkeypatch.setattr(graph, "get_related_facts", lambda eid, **kw: ["good", "wrong-owner", "wrong-scope"])
    rows = [fact("seed"), fact("good"), fact("wrong-owner", owner="other"), fact("wrong-scope", scope="conversation:other")]
    provider(monkeypatch, rows)
    result = await search._graph_expand_candidates(rows[:1], "user", current_channel_id="channel")
    assert [r["id"] for r in result] == ["seed", "good"]
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_core_valid_from_boundary_refills_without_revision(monkeypatch):
    from llm_mem0 import client, search
    clock = NS(now=100)
    fetch = AsyncMock(return_value=[fact("future", importance=5, tier=0, valid_from=101),
                                   fact("scoped", importance=5, tier=0, valid_from=101, scope="conversation:other")])
    monkeypatch.setattr(client, "get_all_memories", fetch)
    monkeypatch.setattr(search, "_read_core_revision", lambda: "fixture")
    monkeypatch.setattr(search.time, "time", lambda: clock.now)
    assert await search.get_core_memories("user", current_channel_id="channel") == []
    clock.now = 101
    assert [r["id"] for r in await search.get_core_memories("user", current_channel_id="channel")] == ["future"]
    assert fetch.await_count == 2
    clock.now = 102
    assert await search.get_core_memories("user", current_channel_id="channel")
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_core_expiry_promotes_eleventh_fact(monkeypatch):
    from llm_mem0 import client, search
    clock = NS(now=100)
    rows = [fact(f"a{i}", importance=5, tier=0, valid_to=101) for i in range(10)]
    rows.append(fact("z-valid", importance=5, tier=0))
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr(client, "get_all_memories", fetch)
    monkeypatch.setattr(search, "_read_core_revision", lambda: "fixture")
    monkeypatch.setattr(search.time, "time", lambda: clock.now)
    assert len(await search.get_core_memories("user")) == 3
    clock.now = 101
    assert [r["id"] for r in await search.get_core_memories("user")] == ["z-valid"]
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_core_malformed_intervals_and_disabled_filter(monkeypatch):
    from llm_mem0 import client, search
    monkeypatch.setattr(search, "_read_core_revision", lambda: "fixture")
    fetch = AsyncMock(return_value=[fact("bad", importance=5, tier=0, valid_from="bad"),
                                   fact("future", importance=5, tier=0, valid_from=253402300799)])
    monkeypatch.setattr(client, "get_all_memories", fetch)
    assert await search.get_core_memories("user") == []
    monkeypatch.setenv("MEM0_HIDE_ARCHIVED", "0")
    assert {r["id"] for r in await search.get_core_memories("user")} == {"bad", "future"}
    assert fetch.await_count == 2


@pytest.mark.parametrize("query", ["猫", "猫を飼っていますか", "飼っていますか"])
@pytest.mark.asyncio
async def test_japanese_rerank_keeps_internal_evidence(monkeypatch, query):
    from llm_mem0 import search
    text = "背景説明。" * 300 + "猫は飼っていません。" + "補足説明。" * 500
    call = AsyncMock(return_value={"top": []})
    monkeypatch.setattr(search, "complete_json", call)
    assert await search._rerank_memories(query, [fact("cat", text)]) == []
    prompt = call.await_args.kwargs["user_message"]
    assert "猫は飼っていません" in prompt


@pytest.mark.asyncio
async def test_rerank_preserves_qualifiers_and_candidate_budget(monkeypatch):
    from llm_mem0 import search
    call = AsyncMock(return_value={"top": [1]})
    monkeypatch.setattr(search, "complete_json", call)
    row = fact("conditional", "本文。" * 1000, scope="conversation:123", valid_from=100,
               valid_to=2000000000, created_at_unix=100, condition="条件説明。" * 140 + "ただし夜間は禁止します。")
    assert await search._rerank_memories("夜間", [row]) == [row]
    prompt = call.await_args.kwargs["user_message"]
    assert "ただし夜間は禁止します" in prompt
    assert "conversation:123" in prompt and "2000000000" in prompt and "created_at_unix" in prompt
    snippet = prompt.split("<untrusted_candidate>", 1)[1].split("</untrusted_candidate>", 1)[0]
    assert len(snippet.replace("{{", "{").replace("}}", "}")) <= 1300


@pytest.mark.parametrize("rerank", [False, True])
@pytest.mark.asyncio
async def test_only_final_result_is_offered_for_recall_reinforcement(monkeypatch, rerank):
    from llm_mem0 import search
    rows = [fact("query-result")]
    core = {**fact("core"), "_core": True}
    monkeypatch.setattr(search, "_expand_queries", AsyncMock(return_value=["query"]))
    monkeypatch.setattr(search, "_search_multi_hybrid", AsyncMock(return_value=rows))
    monkeypatch.setattr(search, "_graph_expand_candidates", AsyncMock(return_value=rows))
    monkeypatch.setattr(search, "get_core_memories", AsyncMock(return_value=[core]))
    monkeypatch.setattr(search, "complete_json", AsyncMock(return_value={"top": [1]}))
    offered = []
    monkeypatch.setattr(search, "_fire_recall_reinforcement", lambda rows: offered.extend(rows))
    result = await search.search_memories_smart("query", "user", 1, rewrite=False, rerank=rerank, include_core=True)
    assert result == offered == [core]


def test_reindex_duplicate_identity_is_idempotent_and_conflicts_preserve_old_index():
    from llm_mem0 import bm25_index as bm
    assert bm.reindex_all([("same", "Orion", "user")] * 20 + [("other", "Orion context", "user")]) == 2
    assert {r["id"] for r in bm.search_bm25("Orion", "user")} == {"same", "other"}
    for conflict in [("same", "different", "user"), ("same", "Orion", "other-owner")]:
        assert bm.reindex_all([("same", "Orion", "user"), conflict]) == 0
        assert {r["id"] for r in bm.search_bm25("Orion", "user")} == {"same", "other"}


@pytest.mark.asyncio
async def test_graph_refills_filtered_siblings_with_bounded_unique_provider_reads(monkeypatch):
    from llm_mem0 import graph, search
    entity = graph.upsert_entity("Seed", "topic", "user")
    rows = [fact("seed"), fact("valid")]
    rows.extend(fact(f"archived-{i}", valid_to=1) for i in range(5))
    for timestamp, row in enumerate(rows, start=1):
        with monkeypatch.context() as clock:
            clock.setattr(graph.time, "time", lambda: timestamp)
            graph.add_edge(row["id"], entity)
    gets = provider(monkeypatch, rows)
    real = graph.get_related_facts
    limits = []
    def related(eid, **kwargs):
        limits.append(kwargs["limit"])
        return real(eid, **kwargs)
    monkeypatch.setattr(graph, "get_related_facts", related)
    result = await search._graph_expand_candidates(rows[:1], "user")
    assert [r["id"] for r in result] == ["seed", "valid"]
    assert limits == [5, 10]
    assert len(gets) == len(set(gets)) == 6


@pytest.mark.asyncio
async def test_graph_refill_stops_at_budget(monkeypatch):
    from llm_mem0 import graph, search
    monkeypatch.setattr(graph, "get_fact_entities", lambda fid: [NS(id=1, user_id="user")])
    monkeypatch.setattr(graph, "get_co_occurring_entities", lambda *a, **kw: [])
    limits = []
    def related(eid, **kwargs):
        limits.append(kwargs["limit"])
        return [str(i) for i in range(kwargs["limit"])]
    monkeypatch.setattr(graph, "get_related_facts", related)
    gets = provider(monkeypatch, [])
    initial = [fact("seed")]
    assert await search._graph_expand_candidates(initial, "user") == initial
    assert limits == [5, 10, 20] and len(gets) == len(set(gets)) == 20


@pytest.mark.asyncio
async def test_filters_only_episode_retains_episode_and_owner_predicate(monkeypatch):
    from llm_mem0 import search
    rows = [fact(str(i), episode_id="other") for i in range(200)]
    rows.extend([fact("target", episode_id="wanted"), fact("foreign", owner="other", episode_id="wanted")])
    calls = []
    class FiltersOnly:
        def get_all(self, *, filters, limit):
            calls.append(filters)
            return [r for r in rows if all((r.get(k) or r["metadata"].get(k)) == v
                                          for k, v in filters.items())][:limit]
    monkeypatch.setattr(search, "_get_mem0", lambda: FiltersOnly())
    assert [r["id"] for r in await search.get_episode_facts("wanted", "user", 15)] == ["target"]
    assert calls == [{"user_id": "user", "episode_id": "wanted"}]


@pytest.mark.asyncio
async def test_episode_legacy_fallback_is_bounded_and_isolates_bad_rows(monkeypatch):
    from llm_mem0 import client, search
    calls = []
    class Legacy:
        def get_all(self, *, user_id, limit):
            calls.append((user_id, limit))
            return [None, fact("bad", episode_id="other"), fact("good", episode_id="wanted"),
                    fact("foreign", owner="other", episode_id="wanted")]
    monkeypatch.setattr(search, "_get_mem0", lambda: Legacy())
    monkeypatch.setattr(client, "_filters_style", False)
    assert [r["id"] for r in await search.get_episode_facts("wanted", "user", 15)] == ["good"]
    assert calls == [("user", 200)]


@pytest.mark.asyncio
async def test_lineage_blocks_pending_repair_in_vector_lexical_and_cached_core(monkeypatch):
    from llm_mem0 import bm25_index as bm, client, lineage, search
    rows = [fact("derived", importance=5, tier=0)]
    provider(monkeypatch, rows, vector=rows)
    assert bm.index_fact("derived", "Orion", "user")
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr(client, "get_all_memories", fetch)
    assert lineage.register_derivation("derived", "user", ["source"], kind="gist", rule="all")
    assert [r["id"] for r in await search.get_core_memories("user")] == ["derived"]
    # Leave the provider metadata untouched, as after a failed repair write.
    lineage.retire_source("source", retired_at=200)
    assert await search.search_memories("Orion", "user") == []
    assert await search.search_memories_hybrid("Orion", "user") == []
    assert await search.get_core_memories("user") == []
    assert fetch.await_count == 2  # lost support triggers one fresh core fill
    past = await search.search_memories_at_time("Orion", "user", 199)
    assert [r["id"] for r in past] == ["derived"]
    assert await search.search_memories_at_time("Orion", "user", 200) == []


@pytest.mark.asyncio
async def test_lineage_or_support_keeps_remaining_source(monkeypatch):
    from llm_mem0 import lineage, search
    rows = [fact("derived")]
    provider(monkeypatch, rows, vector=rows)
    assert lineage.register_derivation("derived", "user", ["a", "b"], kind="schema", rule="any")
    lineage.retire_source("a", retired_at=100)
    assert [r["id"] for r in await search.search_memories("Orion", "user")] == ["derived"]
    lineage.retire_source("b", retired_at=200)
    assert await search.search_memories("Orion", "user") == []
    assert [r["id"] for r in await search.search_memories_at_time("Orion", "user", 150)] == ["derived"]


@pytest.mark.asyncio
async def test_lineage_reads_are_batched_off_loop_and_fail_closed(monkeypatch):
    from llm_mem0 import bm25_index as bm, client, lineage, search
    rows = [fact(str(i), importance=5, tier=0) for i in range(20)]
    provider(monkeypatch, rows, vector=rows)
    calls = []
    loop_thread = threading.get_ident()
    def unsupported(ids, *, as_of=None):
        calls.append((set(ids), threading.get_ident(), as_of))
        return set()
    monkeypatch.setattr(lineage, "unsupported_fact_ids", unsupported)
    assert len(await search.search_memories("Orion", "user", limit=20)) == 20
    assert len(calls) == 1 and len(calls[0][0]) == 20 and calls[0][1] != loop_thread
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic unavailable lineage")
    monkeypatch.setattr(lineage, "unsupported_fact_ids", broken)
    monkeypatch.setattr(client, "get_all_memories", AsyncMock(return_value=rows))
    assert bm.index_fact("0", "Orion", "user")
    assert await search.search_memories("Orion", "user") == []
    assert await search.search_memories_hybrid("Orion", "user") == []
    assert await search.get_core_memories("user") == []
    assert "user" not in search._CORE_CACHE
    monkeypatch.setattr(lineage, "unsupported_fact_ids", unsupported)
    assert len(await search.get_core_memories("user")) == 3


@pytest.mark.asyncio
async def test_lineage_graph_episode_and_final_result_recheck(monkeypatch):
    from llm_mem0 import graph, lineage, search
    assert lineage.register_derivation("derived", "user", ["source"], rule="all")
    lineage.retire_source("source", retired_at=200)
    rows = [fact("seed"), fact("derived", episode_id="episode")]
    provider(monkeypatch, rows)
    monkeypatch.setattr(graph, "get_fact_entities", lambda fid: [NS(id=1, user_id="user")])
    monkeypatch.setattr(graph, "get_co_occurring_entities", lambda *a, **kw: [])
    monkeypatch.setattr(graph, "get_related_facts", lambda *a, **kw: ["derived"])
    assert await search._graph_expand_candidates(rows[:1], "user") == rows[:1]
    monkeypatch.setattr(search, "_get_mem0", lambda: NS(get_all=lambda **kw: rows[1:]))
    assert await search.get_episode_facts("episode", "user") == []
    result = await search._finalize_smart_result(rows[1:], None, 5, {}, fok_block=False)
    assert result == []


@pytest.mark.parametrize("has_replacement", [False, True])
@pytest.mark.asyncio
async def test_core_lineage_boundary_refills_once_without_metadata_repair(monkeypatch, has_replacement):
    from llm_mem0 import client, lineage, search
    clock = NS(now=100)
    monkeypatch.setattr(search.time, "time", lambda: clock.now)
    monkeypatch.setattr(search, "_read_core_revision", lambda: "unchanged")
    rows = [fact(f"a{i}", importance=5, tier=0) for i in range(10)]
    for row in rows:
        assert lineage.register_derivation(row["id"], "user", ["source"], rule="all")
    # Persist a future retirement before filling the cache. The provider
    # metadata deliberately has no valid_to, as with pending repair writes.
    lineage.retire_source("source", retired_at=101)
    if has_replacement:
        rows.append(fact("z-valid", importance=5, tier=0))
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr(client, "get_all_memories", fetch)
    assert len(await search.get_core_memories("user", 10)) == 10
    assert search._CORE_CACHE["user"][3] is None
    clock.now = 101
    expected = ["z-valid"] if has_replacement else []
    assert [r["id"] for r in await search.get_core_memories("user", 10)] == expected
    assert fetch.await_count == 2
    assert [r["id"] for r in await search.get_core_memories("user", 10)] == expected
    assert fetch.await_count == 2  # no repeated fill, including an empty snapshot


@pytest.mark.parametrize("as_of", [None, 100, 101])
@pytest.mark.asyncio
async def test_smart_rerank_rechecks_expiry_and_keeps_explicit_as_of(monkeypatch, as_of):
    from llm_mem0 import search
    clock = NS(now=100)
    monkeypatch.setattr(search.time, "time", lambda: clock.now)
    rows = [fact("expiring", valid_from=90, valid_to=101), fact("stable", valid_from=90)]
    monkeypatch.setattr(search, "_expand_queries", AsyncMock(return_value=["query"]))
    monkeypatch.setattr(search, "_search_multi_hybrid", AsyncMock(return_value=rows))
    monkeypatch.setattr(search, "_graph_expand_candidates", AsyncMock(return_value=rows))
    async def rerank_response(*args, **kwargs):
        await asyncio.sleep(0)
        clock.now = 101
        return {"top": [1, 2]}
    complete = AsyncMock(side_effect=rerank_response)
    monkeypatch.setattr(search, "complete_json", complete)
    offered = []
    monkeypatch.setattr(search, "_fire_recall_reinforcement", lambda result: offered.extend(result))
    token = search._AS_OF.set(as_of)
    try:
        result = await search.search_memories_smart(
            "query", "user", 2, rewrite=False, rerank=True, include_core=False,
        )
    finally:
        search._AS_OF.reset(token)
    complete.assert_awaited_once()
    expected = ["expiring", "stable"] if as_of == 100 else ["stable"]
    assert [r["id"] for r in result] == expected
    assert offered == result


@pytest.mark.parametrize("last_wait", ["core", "lineage"])
@pytest.mark.asyncio
async def test_smart_final_time_check_follows_last_wait(monkeypatch, last_wait):
    from llm_mem0 import search
    clock = NS(now=100)
    monkeypatch.setattr(search.time, "time", lambda: clock.now)
    core = {**fact("core", valid_to=101), "_core": True}
    rows = [fact("query", valid_to=101),
            {**fact("bundle", valid_to=101), "_episode_bundle": True},
            fact("stable", scope="conversation:channel")]
    async def core_result():
        await asyncio.sleep(0)
        if last_wait == "core":
            clock.now = 101
        return [core]
    async def supported(result):
        await asyncio.sleep(0)
        if last_wait == "lineage":
            clock.now = 101
        return result
    monkeypatch.setattr(search, "_filter_supported_facts", supported)
    result = await search._finalize_smart_result(rows, core_result(), 5, {}, fok_block=False)
    assert [r["id"] for r in result] == ["stable"]

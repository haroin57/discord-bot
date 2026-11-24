"""Deterministic alpha-only quality regressions; never use real memories/LLMs."""
import asyncio
import copy
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(autouse=True)
def isolated_alpha(monkeypatch, tmp_path):
    from llm_mem0 import bm25_index, graph, priming, search
    for module, name in ((bm25_index, "facts.sqlite"), (graph, "graph.sqlite")):
        monkeypatch.setattr(module._store, "_path", str(tmp_path / name))
        monkeypatch.setattr(module._store, "_wal_ready_path", None)
    monkeypatch.setattr(graph, "_top_entities_cache", {})
    monkeypatch.setattr(search, "_CORE_CACHE", {})
    monkeypatch.setattr(search, "_CORE_GENERATION", 0)
    monkeypatch.setattr(priming, "_buffer", {})
    monkeypatch.setenv("LLM_MEM0_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MEM0_COLLECTION_NAME", "synthetic_quality")
    monkeypatch.setenv("MEM0_HIDE_ARCHIVED", "1")
    monkeypatch.setenv("MEM0_SCOPE_FILTER_ENABLED", "1")
    monkeypatch.setenv("MEM0_HYBRID_ENABLED", "1")
    monkeypatch.setenv("MEM0_CORE_MEMORY_ENABLED", "1")
    monkeypatch.setattr(search, "_get_mem0", lambda: None)
    monkeypatch.setattr(search, "complete_json", AsyncMock(return_value=None))
    def denied(*a, **kw):
        raise AssertionError("network forbidden in synthetic retrieval tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def fact(fid, *, score=.3, owner="user", metadata=None, text=None):
    return {"id": fid, "memory": text or "Orion project " + fid, "score": score,
            "user_id": owner, "metadata": metadata or {}}


def provider(monkeypatch, rows, *, lexical=None):
    from llm_mem0 import search
    by_id = {row["id"]: row for row in rows}
    calls = []
    def get(fid):
        calls.append(fid)
        return copy.deepcopy(by_id.get(fid))
    monkeypatch.setattr(search, "_get_mem0", lambda: NS(get=get))
    monkeypatch.setattr(search, "mem_search", lambda *a, **kw: copy.deepcopy(rows[:kw["limit"]]))
    return calls


@pytest.mark.asyncio
async def test_hybrid_hydrates_and_rejects_expired_scoped_deleted_wrong_owner(monkeypatch):
    from llm_mem0 import bm25_index as bm, search
    rows = [fact("old", metadata={"valid_to": 1}),
            fact("other-channel", metadata={"scope": "conversation:2"}),
            fact("wrong-owner", owner="another"), fact("good")]
    calls = provider(monkeypatch, rows)
    for fid in [r["id"] for r in rows] + ["deleted"]:
        bm.index_fact(fid, "Orion project " + fid, "user")
    got = await search.search_memories_hybrid("Orion", "user", current_channel_id="1")
    assert [r["id"] for r in got] == ["good"]
    assert "good" not in calls  # rich valid vector hit is reused
    assert set(calls) == {"old", "other-channel", "wrong-owner", "deleted"}
    assert got[0]["retrieval_sources"] == ["bm25", "vector"]


@pytest.mark.asyncio
async def test_valid_lexical_only_candidate_keeps_full_metadata(monkeypatch):
    from llm_mem0 import bm25_index as bm, search
    row = fact("lexical", metadata={"condition": "night", "scope": "conversation:1"})
    provider(monkeypatch, [row])
    monkeypatch.setattr(search, "mem_search", lambda *a, **k: [])
    bm.index_fact("lexical", row["memory"], "user")
    got = await search.search_memories_hybrid("Orion", "user", current_channel_id="1")
    assert len(got) == 1 and got[0]["metadata"] == row["metadata"]
    assert got[0]["score"] is None and got[0]["vector_distance"] is None
    assert search.assess_recall_confidence(got)["best_distance"] is None
    assert search.assess_recall_confidence(got)["status"] == "weak"


@pytest.mark.asyncio
async def test_postfilter_backfills_and_isolates_malformed_rows(monkeypatch):
    from llm_mem0 import search
    rows = [fact("old", metadata={"valid_to": 1}), fact("bad", metadata="bad"), fact("good")]
    calls = []
    provider(monkeypatch, rows)
    def backend(*a, **kw):
        calls.append(kw["limit"])
        return rows[:kw["limit"]]
    monkeypatch.setattr(search, "mem_search", backend)
    got = await search.search_memories("Orion", "user", limit=1)
    assert [r["id"] for r in got] == ["good"]
    assert calls == [1, 2, 4]


def test_merge_best_distance_and_order_independence():
    from llm_mem0 import search
    rows = [[fact("target", score=1.1), fact("distractor", score=.4)], [fact("target", score=.1)]]
    assert search._merge_ranked_results(rows, 1)[0]["id"] == "target"
    assert search._merge_ranked_results(rows, 2) == search._merge_ranked_results(rows[::-1], 2)


def test_rrf_ranking_survives_multi_merge_and_duplicates_do_not_vote_twice():
    from llm_mem0 import bm25_index as bm, search
    vector = [fact("vector", score=.1), fact("hybrid", score=.9)]
    lexical = [{"id": "hybrid", "rank": 1}]
    fused = bm.rrf_merge(vector, lexical)
    assert search._merge_ranked_results([fused], 2)[0]["id"] == "hybrid"
    assert bm.rrf_merge(vector, lexical * 2) == fused


@pytest.mark.parametrize("result,expected", [
    ({"top": []}, []), ({"top": [1, 1, 2]}, ["a", "b"]),
    ({"top": [True, "1", 1.5, -1, 99, 2]}, ["b"]),
    ({"top": "1"}, ["a", "b"]), (None, ["a", "b"]),
])
@pytest.mark.asyncio
async def test_rerank_empty_duplicate_malformed_contract(monkeypatch, result, expected):
    from llm_mem0 import search
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(search, "complete_json", call)
    got = await search._rerank_memories("query", [fact("a"), fact("b")], top_k=2)
    assert [r["id"] for r in got] == expected
    assert call.await_count == 1  # <= K still permits abstention


@pytest.mark.asyncio
async def test_rerank_keeps_tail_and_conditions(monkeypatch):
    from llm_mem0 import search
    call = AsyncMock(return_value={"top": []})
    monkeypatch.setattr(search, "complete_json", call)
    row = fact("a", text="prefix " * 200 + " decisive correction", metadata={"condition": "night only"})
    await search._rerank_memories("correction", [row], top_k=2)
    prompt = call.await_args.kwargs["user_message"]
    assert "decisive correction" in prompt and "night only" in prompt


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, "0.1"])
def test_non_numeric_or_nonfinite_distance_is_not_confidence(score):
    from llm_mem0 import search
    assert search.assess_recall_confidence([{"score": score}])["status"] == "no_memory"


def test_boost_preserves_provider_score_and_graph_not_cosine(monkeypatch):
    from llm_mem0 import reinforce, search
    monkeypatch.setattr(reinforce, "reinforcement_boost", lambda md: .2)
    rows = [fact("a", score=.6)]
    search._apply_reinforcement_boost(rows)
    search._apply_reinforcement_boost(rows)
    assert rows[0]["score"] == .6 and rows[0]["ranking_distance"] == pytest.approx(.4)
    assert search.assess_recall_confidence([{"score": .3, "_graph_expanded": True}])["best_distance"] is None


@pytest.mark.parametrize("text,query,negative", [
    ("Python 3.12で開発する", "3.12", "3.11"),
    ("C言語が得意です", "C", "C++"),
    ("C++言語が得意です", "C++", "C"),
    ("C#を使います", "C#", "C++"),
    ("Rで統計分析する", "R", "C"),
    ("誕生日は5月7日です", "5月7日", "7月5日"),
    ("猫を飼っています", "猫", "犬"),
    ("ＰｏｓｔｇｒｅＳＱＬを使います", "PostgreSQL", "SQLite"),
])
def test_exact_multilingual_terms(text, query, negative):
    from llm_mem0 import bm25_index as bm
    assert bm.index_fact("f", text, "user")
    assert [r["id"] for r in bm.search_bm25(query, "user")] == ["f"]
    assert bm.search_bm25(negative, "user") == []
    assert bm.search_bm25(query, "other-user") == []


def test_legacy_index_version_is_explicit_and_not_silently_rebuilt():
    from llm_mem0 import bm25_index as bm
    bm._store.write(lambda c: c.execute(
        "INSERT INTO facts_fts(fact_id,user_id,orig_text,text) VALUES(?,?,?,?)",
        ("legacy", "user", "C language", "language")))
    assert bm.stats()["legacy_tokenizer_rows"] == 1
    assert bm.search_bm25("C", "user") == []
    assert bm.stats()["legacy_tokenizer_rows"] == 1
    bm.index_fact("legacy", "C language", "user")
    assert bm.stats()["legacy_tokenizer_rows"] == 0
    assert bm.search_bm25("C", "user")[0]["id"] == "legacy"


def test_entity_identity_no_global_person_invention_or_type_merge():
    from llm_mem0 import graph
    boss = graph.upsert_entity("boss", "person", "user")
    assert graph.find_entity("boss", "user").canonical_name == "boss"
    fruit = graph.upsert_entity("Apple", "food", "user")
    assert fruit != boss and graph.upsert_entity("Apple", "company", "user") == 0
    assert graph.find_entity("Apple", "user").type == "food"
    rust = graph.upsert_entity("Rust", "language", "user")
    assert graph.upsert_entity("Ｒｕｓｔ", "language", "user") == rust
    assert graph.find_entity("rust", "user").id == rust


def test_graph_source_fact_reprojection_is_idempotent():
    from llm_mem0 import graph
    entity = graph.upsert_entity("Rust", "language", "user", source_fact_id="fact-1")
    for _ in range(3):
        assert graph.upsert_entity("Rust", "language", "user", source_fact_id="fact-1") == entity
    assert graph.find_entity("Rust", "user").mention_count == 1
    graph.upsert_entity("Rust", "language", "user", source_fact_id="fact-2")
    assert graph.find_entity("Rust", "user").mention_count == 2
    graph.delete_fact_edges("fact-2")
    graph.delete_fact_edges("fact-2")
    assert graph.find_entity("Rust", "user").mention_count == 1


def test_graph_legacy_edge_event_backfill_does_not_reinforce():
    from llm_mem0 import graph
    entity = graph.upsert_entity("Rust", "language", "user")
    graph.add_edge("fact-1", entity)
    graph.upsert_entity("Rust", "language", "user", source_fact_id="fact-1")
    assert graph.find_entity("Rust", "user").mention_count == 1


@pytest.mark.asyncio
async def test_phrase_alias_expansion_preserves_question_and_negation():
    from llm_mem0 import graph, search
    graph.upsert_entity("Blade Runner", "film", "user", aliases=["ブレードランナー"])
    got = await search._expand_query_via_graph("ブレードランナーは好きではないと話した", "user")
    assert "Blade Runnerは好きではないと話した" in got
    assert await search._expand_query_via_graph("ブレードランナーは好き", "another") == []


def test_relation_owner_and_future_validity():
    from llm_mem0 import graph
    a = graph.upsert_entity("Alice", "person", "user")
    b = graph.upsert_entity("Org", "org", "user")
    outsider = graph.upsert_entity("Outsider", "person", "other")
    assert graph.add_relation(a, "knows", outsider, "user") == 0
    rid = graph.add_relation(a, "joins", b, "user", valid_from=int(time.time()) + 86400)
    assert rid and graph.get_relations(a) == []
    assert len(graph.get_relations(a, only_valid=False)) == 1


def test_graph_normalization_ranks_before_limit_and_excludes_other_owners():
    from llm_mem0 import graph
    seed = graph.upsert_entity("Seed", "topic", "user")
    rare = graph.upsert_entity("Rare", "topic", "user")
    other = graph.upsert_entity("Other", "topic", "other-user")
    for fid in ("a", "b"):
        graph.add_edge(fid, seed)
    graph.add_edge("a", rare)
    graph.add_edge("a", other)
    for i in range(4):
        hub = graph.upsert_entity("Hub" + str(i), "topic", "user")
        for fid in ("a", "b"):
            graph.add_edge(fid, hub)
        for j in range(25):
            graph.add_edge(f"hub-{i}-{j}", hub)
    assert graph.get_co_occurring_entities(seed, limit=1)[0][0] == rare
    assert other not in [row[0] for row in graph.get_co_occurring_entities(seed)]


@pytest.mark.asyncio
async def test_core_scope_before_cap_and_invalidation(monkeypatch):
    from llm_mem0 import client, search
    rows = [fact(f"scoped-{i}", metadata={"importance": 5, "tier": 0,
            "scope": "conversation:2", "last_reinforced_at": 100}) for i in range(12)]
    rows.append(fact("global", metadata={"importance": 5, "tier": 0}))
    call = AsyncMock(return_value=rows)
    monkeypatch.setattr(client, "get_all_memories", call)
    assert [r["id"] for r in await search.get_core_memories("user", current_channel_id="1")] == ["global"]
    assert await search.get_core_memories("user", current_channel_id="2")
    assert call.await_count == 1
    await asyncio.to_thread(search.invalidate_core_cache, "user")
    await search.get_core_memories("user", current_channel_id="1")
    assert call.await_count == 2
    monkeypatch.setenv("MEM0_COLLECTION_NAME", "another")
    await search.get_core_memories("user", current_channel_id="1")
    assert call.await_count == 3


@pytest.mark.asyncio
async def test_invalidation_during_fetch_does_not_publish_stale_core(monkeypatch):
    from llm_mem0 import client, search
    async def fetch(uid):
        await asyncio.to_thread(search.invalidate_core_cache, uid)
        return [fact("obsolete", metadata={"importance": 5, "tier": 0})]
    monkeypatch.setattr(client, "get_all_memories", fetch)
    assert await search.get_core_memories("user") == []
    assert not search._CORE_CACHE


@pytest.mark.asyncio
async def test_another_process_invalidates_cached_core_without_waiting_for_ttl(monkeypatch):
    from llm_mem0 import client, search
    call = AsyncMock(return_value=[fact("old", metadata={"importance": 5, "tier": 0})])
    monkeypatch.setattr(client, "get_all_memories", call)
    assert (await search.get_core_memories("user"))[0]["id"] == "old"
    assert call.await_count == 1
    command = [sys.executable, "-c", "from llm_mem0.search import invalidate_core_cache; invalidate_core_cache('user')"]
    completed = await asyncio.to_thread(subprocess.run, command, env=dict(os.environ),
                                        capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    call.return_value = [fact("new", metadata={"importance": 5, "tier": 0})]
    assert (await search.get_core_memories("user"))[0]["id"] == "new"
    assert call.await_count == 2


@pytest.mark.asyncio
async def test_core_revision_reads_are_off_event_loop(monkeypatch):
    from llm_mem0 import client, search
    loop_thread = threading.get_ident()
    reads = []
    def revision():
        reads.append(threading.get_ident())
        return "fixture"
    monkeypatch.setattr(search, "_read_core_revision", revision)
    monkeypatch.setattr(client, "get_all_memories", AsyncMock(return_value=[]))
    await search.get_core_memories("user")
    await search.get_core_memories("user")
    assert reads and all(thread != loop_thread for thread in reads)


@pytest.mark.asyncio
async def test_graph_owner_and_unrelated_priming_are_excluded(monkeypatch):
    from llm_mem0 import graph, priming, search
    monkeypatch.setattr(graph, "get_fact_entities", lambda fid: [NS(id=1, user_id="user"), NS(id=2, user_id="other")])
    monkeypatch.setattr(graph, "get_co_occurring_entities", lambda *a, **kw: [])
    seen = []
    def related(eid, **kw):
        seen.append(eid)
        return ["other-owner"]
    monkeypatch.setattr(graph, "get_related_facts", related)
    monkeypatch.setattr(priming, "get_primed_entities", lambda *a: {99})
    monkeypatch.setattr(search, "_get_mem0", lambda: NS(get=lambda fid: fact(fid, owner="other")))
    rows = [fact("seed")]
    assert await search._graph_expand_candidates(rows, "user", current_channel_id="1") == rows
    assert seen == [1]


def test_priming_keeps_channel_user_and_collection_boundaries(monkeypatch):
    from llm_mem0 import priming
    priming.touch("user", "channel", [1])
    assert priming.get_primed_entities("user", "channel") == {1}
    assert priming.get_primed_entities("other", "channel") == set()
    assert priming.get_primed_entities("user", "other") == set()
    monkeypatch.setenv("MEM0_COLLECTION_NAME", "another")
    assert priming.get_primed_entities("user", "channel") == set()


@pytest.mark.asyncio
async def test_time_api_hybrid_intervals_and_request_isolation(monkeypatch):
    from llm_mem0 import bm25_index as bm, search
    rows = [fact("past", metadata={"valid_from": 100, "valid_to": 200}),
            fact("future", metadata={"valid_from": 200}),
            fact("scoped", metadata={"scope": "conversation:2"})]
    provider(monkeypatch, rows)
    for row in rows:
        bm.index_fact(row["id"], row["memory"], "user")
    past, future = await asyncio.gather(
        search.search_memories_at_time("Orion", "user", 150, current_channel_id="1"),
        search.search_memories_at_time("Orion", "user", "1970-01-01T00:03:20Z", current_channel_id="1"))
    assert [r["id"] for r in past] == ["past"]
    assert [r["id"] for r in future] == ["future"]
    assert search._AS_OF.get() is None


@pytest.mark.parametrize("value", [True, 1.5, -1, "2026-09-09", "bad", None])
@pytest.mark.asyncio
async def test_time_api_rejects_ambiguous_or_invalid_time(value):
    from llm_mem0 import search
    with pytest.raises(ValueError):
        await search.search_memories_at_time("Orion", "user", value)


@pytest.mark.asyncio
async def test_expansion_rejects_wrong_json_shapes(monkeypatch):
    from llm_mem0 import search
    monkeypatch.setattr(search, "complete_json", AsyncMock(return_value={"queries": "Alice", "keywords": "Alice", "aliases": {"x": "y"}}))
    assert await search._rewrite_query("long question") == []
    assert await search._extract_keywords_via_llm("long question") == ""


def test_format_preserves_author_time_and_does_not_claim_reconfirmation():
    from llm_mem0 import format as fmt
    text = fmt.format_history_for_prompt([{"memory": "Orion retired", "role": "assistant",
                                         "author": "Kanae", "ts": "2020-01-01", "source_id": "transcript:fixture:assistant"}])
    assert "Kanae" in text and "2020-01-01" in text and "(私)" not in text
    assert "transcript:fixture:assistant" in text and "discord.com" not in text
    suffix = fmt._fact_meta_suffix({"metadata": {"created_at_unix": 1_600_000_000,
                                                "last_reinforced_at": 1_700_000_000,
                                                "condition": "x" * 60 + "but not at night"}})
    assert "but not at night" in suffix and "最終参照・強化" in suffix
    assert "[確認:" not in suffix and "[記録:" in suffix

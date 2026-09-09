"""Second-stage lifecycle acceptance: synthetic primary providers + real SQLite."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import socket
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, bootstrap):
    from llm_mem0 import bm25_index, client, fact_store, graph, history_embed, history_index, lineage, search
    from llm_mem0.sqlite_store import SqliteStore
    monkeypatch.setenv("LLM_MEM0_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MEM0_COLLECTION_NAME", "test-lifecycle")
    for module in (bm25_index, fact_store, graph, history_index):
        store = SqliteStore(module._store._filename, module._SCHEMA)
        store.reset_path_for_test(str(tmp_path / store._filename))
        monkeypatch.setattr(module, "_store", store)
    monkeypatch.setattr(lineage, "_stores", {})
    monkeypatch.setattr(history_embed, "_state", {"col": None})
    monkeypatch.setattr(search, "invalidate_core_cache", lambda *args: None)
    monkeypatch.setattr(client, "_get_mem0", lambda: None)
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden in lifecycle acceptance")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture(scope="module", autouse=True)
def bootstrap():
    import services.mem0 as package
    return package


class Memory:
    def __init__(self, rows=()):
        self.rows = {r["id"]: deepcopy(r) for r in rows}
        self.fail_update = set()
        self.add_calls = 0
        self.commit_timeout = False

    def get(self, memory_id):
        return deepcopy(self.rows.get(memory_id))

    def get_all(self, *, user_id, limit=None):
        return {"results": deepcopy([r for r in self.rows.values() if r["user_id"] == user_id][:limit])}

    def add(self, messages, *, user_id, metadata, infer):
        self.add_calls += 1
        fid = f"gist-{self.add_calls}"
        self.rows[fid] = {"id": fid, "user_id": user_id, "memory": messages[0]["content"],
                          "metadata": deepcopy(metadata)}
        if self.commit_timeout:
            self.commit_timeout = False
            raise TimeoutError("synthetic post-commit timeout")
        return {"results": [{"id": fid}]}

    def update(self, *, memory_id, data, metadata):
        if memory_id in self.fail_update:
            raise RuntimeError("synthetic update failure")
        self.rows[memory_id].update(memory=data, metadata=deepcopy(metadata))

    def delete(self, memory_id):
        self.rows.pop(memory_id, None)


def fact(fid, **metadata):
    return {"id": fid, "user_id": "u", "memory": "synthetic observation " + fid,
            "metadata": {"importance": 2, "tier": 3, "scope": "global", "condition": "",
                         "category": "taste", **metadata}}


def use_memory(monkeypatch, memory):
    from llm_mem0 import client
    monkeypatch.setattr(client, "_get_mem0", lambda: memory)


def history_user(text="original", source="s:user", ts="2026-09-09", user="u"):
    from llm_mem0 import history_index as h
    assert h.index_line(7, ts, "user", text, author_id=user, source_id=source)


@pytest.mark.asyncio
async def test_c1_edited_source_and_backfill_are_replayed_with_revision_ids(monkeypatch):
    from llm_mem0 import history_index as h, ingest, replay
    add = AsyncMock(return_value={"status": "ok", "kept": 1})
    monkeypatch.setattr(ingest, "add_memories", add)
    history_user()
    assert (await replay.replay_channel_history(7, "u", dry_run=False))["turns_processed"] == 1
    first_id = add.await_args.kwargs["metadata"]["request_id"]
    history_user("corrected")
    history_user("backfill", "back:user", ts="2020-01-01")
    assert (await replay.replay_channel_history(7, "u", dry_run=False))["turns_processed"] == 2
    assert [call.kwargs["user_text"] for call in add.await_args_list] == ["original", "corrected", "backfill"]
    assert add.await_args_list[1].kwargs["metadata"]["request_id"] != first_id
    history_user("corrected")
    assert (await replay.replay_channel_history(7, "u", dry_run=False))["turns_processed"] == 0
    assert h.search_history("corrected", 7)[0]["author_id"] == "u"


@pytest.mark.asyncio
async def test_c2_crossed_and_late_replies_are_joined_only_by_source(monkeypatch):
    from llm_mem0 import history_index as h, ingest, replay
    add = AsyncMock(return_value={"status": "ok", "kept": 0})
    monkeypatch.setattr(ingest, "add_memories", add)
    history_user("question A", "A:user", user="a")
    history_user("question B", "B:user", user="b")
    h.index_line(7, "2026-09-09", "assistant", "answer A", source_id="A:assistant")
    h.index_line(7, "2026-09-09", "assistant", "answer B", source_id="B:assistant")
    await replay.replay_channel_history(7, "a", dry_run=False)
    assert add.await_count == 1 and add.await_args.kwargs["assistant_text"] == "answer A"
    old_request = add.await_args.kwargs["metadata"]["request_id"]
    h.index_line(7, "2026-09-09", "assistant", "revised answer A", source_id="A:assistant")
    await replay.replay_channel_history(7, "a", dry_run=False)
    assert add.await_count == 2 and add.await_args.kwargs["assistant_text"] == "revised answer A"
    assert add.await_args.kwargs["metadata"]["request_id"] != old_request


@pytest.mark.asyncio
async def test_c3_reindex_fences_old_inflight_checkpoint(monkeypatch):
    from llm_mem0 import history_index as h, ingest, replay
    history_user()
    entered, release = asyncio.Event(), asyncio.Event()
    async def add(**kwargs):
        entered.set()
        await release.wait()
        return {"status": "ok", "kept": 0}
    monkeypatch.setattr(ingest, "add_memories", add)
    running = asyncio.create_task(replay.replay_channel_history(7, "u", dry_run=False))
    await entered.wait()
    assert h.reindex_all([(7, "2020-01-01", "user", "replacement", "", "u", "new:user")]) == 1
    release.set()
    assert (await running)["stale_generation"]
    assert h.get_replay_cursor(7, "u") == ("", 0)
    assert (await replay.replay_channel_history(7, "u", dry_run=False))["turns_processed"] == 1


def test_c4_legacy_writer_row_reuse_cannot_inherit_author_or_source():
    from llm_mem0 import history_index as h
    h.index_line(7, "t", "user", "same words", "Alice", author_id="alice", source_id="A:user")
    with h._store.connect() as c:
        c.execute("DELETE FROM history_fts")
        c.execute("INSERT INTO history_fts VALUES (?,?,?,?,?,?)", ("7", "t", "user", "Bob", "same words", "same words"))
        c.commit()
    for row in [h.search_history("words", 7)[0], h.get_lines_since(7)[0], h.get_replay_page(7, "alice")["lines"][0]]:
        assert row["author"] == "Bob" and row["author_id"] == "" and row["source_id"] == ""
    assert h.get_source_line(7, "A:user", "user", "alice") is None


@pytest.mark.parametrize("api", ["record", "supersede"])
def test_c5_multistep_cycle_rejected_without_changes(api):
    from llm_mem0 import fact_store as f
    f.record_fact("A", "u", "residence")
    f.record_fact("B", "u", "residence", supersedes="A")
    f.record_fact("C", "u", "residence", supersedes="B")
    before = f.get_history("u", "residence")
    changed = (f.record_fact("A", "u", "residence", supersedes="C") if api == "record"
               else f.supersede_fact("C", "A"))
    assert not changed and f.get_history("u", "residence") == before
    assert [r["fact_id"] for r in f.get_active_facts("u", "residence")] == ["C"]


def test_c5_opposing_concurrent_links_cannot_commit_cycle():
    from llm_mem0 import fact_store as f
    f.record_fact("A", "u", "residence")
    f.record_fact("B", "u", "residence")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda pair: f.supersede_fact(*pair), [("A", "B"), ("B", "A")]))
    assert sorted(results) == [False, True]


def setup_gist(monkeypatch, memory, generate=None):
    from llm_mem0 import dedup, graph
    use_memory(monkeypatch, memory)
    monkeypatch.setattr(graph, "get_fact_entities", lambda fid: [NS(id=1, mention_count=1, type="topic")])
    monkeypatch.setattr(graph, "add_edge", lambda *args: True)
    generate = generate or AsyncMock(return_value=["synthetic gist"])
    monkeypatch.setattr(dedup, "synthesize_cluster", generate)
    return generate


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"tier": 0, "mention_count": 5}, {"importance": 5}, {"valid_from": 4_000_000_000}])
async def test_c6_changed_source_never_archived_during_gist(monkeypatch, change):
    from llm_mem0 import decay
    memory = Memory([fact(str(i)) for i in range(3)])
    async def generate(texts):
        memory.rows["0"]["metadata"].update(change)
        return ["synthetic gist"]
    setup_gist(monkeypatch, memory, generate)
    result = await decay.gist_compress_for_user("u", dry_run=False)
    assert not memory.rows["0"]["metadata"].get("valid_to")
    assert result["archived"] == (0 if "valid_from" in change else 2)
    if "valid_from" in change:
        assert memory.add_calls == 0


@pytest.mark.asyncio
async def test_c7_partial_archive_repairs_below_cluster_threshold_after_restart(monkeypatch):
    from llm_mem0 import decay, lineage
    memory = Memory([fact(str(i)) for i in range(3)])
    generate = setup_gist(monkeypatch, memory)
    memory.fail_update = {"1", "2"}
    assert (await decay.gist_compress_for_user("u", dry_run=False))["archived"] == 1
    monkeypatch.setattr(lineage, "_stores", {})
    memory.fail_update.clear()
    assert (await decay.gist_compress_for_user("u", dry_run=False))["archived"] == 2
    assert generate.await_count == memory.add_calls == 1
    assert lineage.pending_jobs("u", 100) == []


@pytest.mark.asyncio
async def test_c7_gist_commit_timeout_repairs_without_duplicate(monkeypatch):
    from llm_mem0 import decay
    memory = Memory([fact(str(i)) for i in range(3)])
    generate = setup_gist(monkeypatch, memory)
    memory.commit_timeout = True
    assert (await decay.gist_compress_for_user("u", dry_run=False))["archived"] == 0
    assert (await decay.gist_compress_for_user("u", dry_run=False))["archived"] == 3
    assert generate.await_count == memory.add_calls == 1


@pytest.mark.asyncio
async def test_c7_concurrent_gist_and_cancelled_waiter_keep_worker_lease(monkeypatch):
    from llm_mem0 import decay
    memory = Memory([fact(str(i)) for i in range(3)])
    entered, release = threading.Event(), threading.Event()
    original = memory.add
    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    memory.add = blocking
    setup_gist(monkeypatch, memory)
    first = asyncio.create_task(decay.gist_compress_for_user("u", dry_run=False))
    assert await asyncio.to_thread(entered.wait, 10)
    first.cancel()
    second = asyncio.create_task(decay.gist_compress_for_user("u", dry_run=False))
    await asyncio.sleep(0.04)
    assert not first.done() and memory.add_calls == 0
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert memory.add_calls == 1


@pytest.mark.asyncio
async def test_c8_future_sources_do_not_become_current_schema_or_gist(monkeypatch):
    from llm_mem0 import decay, graph, ingest, replay
    memory = Memory([fact(str(i), valid_from=4_000_000_000) for i in range(3)])
    generate = setup_gist(monkeypatch, memory)
    monkeypatch.setattr(graph, "get_fact_entities", lambda fid: [NS(id=int(fid), mention_count=1, type="topic")])
    schema = AsyncMock(return_value=["future schema"])
    add = AsyncMock(return_value={"status": "ok", "kept": 1})
    monkeypatch.setattr(replay, "_generate_schema_facts", schema)
    monkeypatch.setattr(ingest, "add_memories", add)
    assert (await replay.abstract_schemas_for_user("u", dry_run=False))["schemas_created"] == 0
    assert (await decay.gist_compress_for_user("u", dry_run=False))["compressed"] == 0
    assert not add.called and not generate.called and not schema.called


@pytest.mark.asyncio
async def test_c8_schema_inherits_supported_interval_and_lineage(monkeypatch):
    from llm_mem0 import graph, ingest, lineage, replay
    now = int(time.time())
    memory = Memory([fact(str(i), valid_from=now - 100 + i, valid_to=now + 100 + i) for i in range(3)])
    use_memory(monkeypatch, memory)
    monkeypatch.setattr(graph, "get_fact_entities", lambda fid: [NS(id=int(fid), mention_count=1, type="topic")])
    monkeypatch.setattr(replay, "_generate_schema_facts", AsyncMock(return_value=["schema"]))
    add = AsyncMock(return_value={"status": "ok", "kept": 1, "results": [{"results": [{"id": "derived"}]}]})
    monkeypatch.setattr(ingest, "add_memories", add)
    await replay.abstract_schemas_for_user("u", dry_run=False)
    generated = add.await_args.kwargs["pre_extracted_facts"][0]
    assert generated["valid_from"] == now - 98 and generated["valid_to"] == now + 100
    with lineage._store().connect() as c:
        assert c.execute("SELECT count(*) FROM derivation_sources").fetchone()[0] == 3


def test_c9_source_delete_suspends_last_supported_derivation_and_transitive_descendant():
    from llm_mem0 import lineage
    memory = Memory([fact("A"), fact("B"), fact("derived"), fact("child")])
    lineage.register_derivation("derived", "u", ["A", "B"])
    lineage.register_derivation("child", "u", ["derived"])
    memory.delete("A")
    lineage.retire_source_and_repair_sync(memory, "A", "u")
    assert not memory.rows["derived"]["metadata"].get("valid_to")
    memory.delete("B")
    assert lineage.retire_source_and_repair_sync(memory, "B", "u")["failed"] == 0
    assert all(memory.rows[fid]["metadata"].get("valid_to") for fid in ("derived", "child"))


def test_c9_explicit_legacy_lineage_and_update_failure_repair():
    from llm_mem0 import lineage
    memory = Memory([fact("derived", derived_from="deleted", source="consolidation_schema")])
    memory.fail_update = {"derived"}
    assert lineage.retire_source_and_repair_sync(memory, "deleted", "u")["failed"] == 1
    memory.fail_update.clear()
    assert lineage.repair_dependents_sync(memory)["completed"] == 1
    assert memory.rows["derived"]["metadata"]["derived_status"] == "unsupported"


def test_c9_retirement_before_derivation_and_cycles_are_fenced():
    from llm_mem0 import lineage
    lineage.retire_source("deleted")
    assert not lineage.register_derivation("new", "u", ["deleted"])
    with pytest.raises(ValueError, match="cycle"):
        lineage.register_derivation("deleted", "u", ["new"])


@pytest.mark.asyncio
async def test_c9_public_delete_connects_lineage_and_retains_unsupported_body_as_history(monkeypatch):
    from llm_mem0 import client
    memory = Memory([fact("source"), fact("derived", source="consolidation_schema", derived_from="source")])
    use_memory(monkeypatch, memory)
    assert await client.delete_memory("source")
    assert "source" not in memory.rows
    assert memory.rows["derived"]["metadata"]["derived_status"] == "unsupported"
    assert memory.rows["derived"]["metadata"]["valid_to"]


def test_c9_pending_gate_is_temporal_and_transitive_before_provider_repair():
    from llm_mem0 import lineage
    lineage.register_derivation("derived", "u", ["source"])
    lineage.register_derivation("child", "u", ["derived"])
    lineage.retire_source("source", retired_at=100)
    assert lineage.unsupported_fact_ids(["derived", "child", "unknown"], as_of=99) == set()
    assert lineage.unsupported_fact_ids(["derived", "child", "unknown"], as_of=100) == {"derived", "child"}
    assert lineage.has_pending_repairs()


def test_c9_legacy_discovery_resumes_after_outage_without_deleted_primary_owner(monkeypatch):
    from llm_mem0 import lineage
    memory = Memory([fact("derived", derived_from="deleted")])
    original = memory.get_all
    memory.get_all = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("temporary enumeration failure"))
    first = lineage.retire_source_and_repair_sync(memory, "deleted", "u")
    assert first["failed"] and first["pending"] and lineage.has_pending_repairs()
    monkeypatch.setattr(lineage, "_stores", {})
    memory.get_all = original
    final = lineage.repair_dependents_sync(memory)
    assert final["pending"] == final["failed"] == 0 and not lineage.has_pending_repairs()
    assert memory.rows["derived"]["metadata"]["derived_status"] == "unsupported"


def test_c9_legacy_discovery_pages_have_explicit_limits_and_pending_progress():
    from llm_mem0 import lineage
    memory = Memory([fact(str(i), derived_from="deleted" if i == 5 else "") for i in range(6)])
    limits = []
    original = memory.get_all
    def bounded(**kwargs):
        assert kwargs["limit"] is not None
        limits.append(kwargs["limit"])
        return original(**kwargs)
    memory.get_all = bounded
    assert lineage.retire_source_and_repair_sync(memory, "deleted", "u", limit=2)["pending"] == 1
    assert lineage.repair_dependents_sync(memory, limit=2)["pending"] == 1
    assert lineage.repair_dependents_sync(memory, limit=2)["pending"] == 0
    assert limits == [3, 5, 7]
    assert memory.rows["5"]["metadata"]["derived_status"] == "unsupported"


@pytest.mark.parametrize("deleted", [False, True])
def test_c10_retired_source_cannot_reproject_edges_or_mentions(deleted):
    from llm_mem0 import graph as g
    entity = g.upsert_entity("synthetic name", "person", "u", source_fact_id="old")
    assert g.add_edge("old", entity)
    if deleted:
        assert g.delete_fact_edges("old") == 1
    else:
        assert g.expire_relations_for_fact("old")
        with g._store.connect() as c:
            c.execute("DELETE FROM fact_entity_edges WHERE fact_id='old'")
            c.commit()
    before = g.find_entity("synthetic name", "u").mention_count
    assert g.add_edge("old", entity)
    assert g.upsert_entity("synthetic name", "person", "u", source_fact_id="old") == 0
    assert g.get_related_facts(entity) == []
    assert g.find_entity("synthetic name", "u").mention_count == before


def test_c11_semantic_namespace_isolates_same_source_without_renaming_or_deleting(monkeypatch, tmp_path):
    from llm_mem0 import history_embed as h
    class Collection:
        def __init__(self):
            self.rows = {"legacy": {"document": "legacy retained", "metadata": {"channel_id": "7"}}}
            self.where = []
        def upsert(self, *, ids, documents, metadatas, embeddings):
            self.rows[ids[0]] = {"document": documents[0], "metadata": metadatas[0]}
        def query(self, **kwargs):
            self.where.append(kwargs["where"])
            return {"ids": [list(self.rows)], "documents": [[r["document"] for r in self.rows.values()]],
                    "metadatas": [[r["metadata"] for r in self.rows.values()]], "distances": [[0.1] * len(self.rows)]}
    collections = {}
    chroma = NS(get_or_create_collection=lambda *, name, **kwargs: collections.setdefault(name, Collection()))
    use_memory(monkeypatch, NS(vector_store=NS(client=chroma), embedding_model=NS(embed=lambda *args: [0.1])))
    monkeypatch.setattr(h, "_enabled", lambda: True)
    for state, collection, text in [("A", "X", "AX"), ("B", "X", "BX"), ("A", "Y", "AY")]:
        monkeypatch.setenv("LLM_MEM0_STATE_DIR", str(tmp_path / state))
        monkeypatch.setenv("MEM0_COLLECTION_NAME", collection)
        h.index_line(7, "t", "user", "u", text, author_id="u", source_id="s:user")
    monkeypatch.setenv("LLM_MEM0_STATE_DIR", str(tmp_path / "A"))
    monkeypatch.setenv("MEM0_COLLECTION_NAME", "X")
    assert [r["memory"] for r in h.search_semantic("fixture", 7)] == ["AX"]
    assert list(collections) == ["history_transcripts"]
    collection = collections["history_transcripts"]
    assert len(collection.rows) == 4 and "legacy" in collection.rows
    assert collection.where[-1] == {"$and": [{"channel_id": "7"}, {"namespace": h._namespace()}]}


def test_private_lifecycle_state_rejects_symlink_and_uses_mode_600(tmp_path):
    from llm_mem0 import lineage
    old_umask = os.umask(0o022)
    try:
        lineage.register_derivation("derived", "u", ["source"])
    finally:
        os.umask(old_umask)
    path = tmp_path / "mem0_lifecycle.sqlite"
    assert path.stat().st_mode & 0o777 == 0o600
    target = tmp_path / "symlink-target.sqlite"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(OSError):
        lineage.register_derivation("other", "u", ["source"])


def test_failed_head_repair_does_not_starve_healthy_tail():
    from llm_mem0 import lineage
    memory = Memory([fact(f"derived-{i}") for i in range(3)])
    for i in range(3):
        lineage.register_derivation(f"derived-{i}", "u", [f"source-{i}"])
        lineage.retire_source(f"source-{i}", retired_at=100)
    memory.fail_update = {"derived-0"}
    for _ in range(4):
        lineage.repair_dependents_sync(memory, limit=1)
    assert not memory.rows["derived-0"]["metadata"].get("valid_to")
    assert all(memory.rows[f"derived-{i}"]["metadata"].get("valid_to") for i in (1, 2))


def test_failed_head_discovery_does_not_starve_next_owner():
    from llm_mem0 import lineage
    memory = Memory([fact("derived", derived_from="tail")])
    for i in range(4):
        lineage.retire_source(f"bad-{i}", user_id=f"bad-owner-{i}")
    lineage.retire_source("tail", user_id="u")
    original = memory.get_all
    def provider(**kwargs):
        if kwargs["user_id"] != "u":
            raise RuntimeError("owner-specific temporary outage")
        return original(**kwargs)
    memory.get_all = provider
    lineage.repair_dependents_sync(memory)
    lineage.repair_dependents_sync(memory)
    assert memory.rows["derived"]["metadata"]["derived_status"] == "unsupported"


def test_large_retirement_fanout_is_bounded_but_unpropagated_descendant_is_gated():
    from llm_mem0 import lineage
    for i in range(130):
        lineage.register_derivation(f"derived-{i:03}", "u", ["source"])
    lineage.register_derivation("tail-child", "u", ["derived-129"])
    lineage.retire_source("source", retired_at=100)
    with lineage._store().connect() as c:
        assert c.execute("SELECT count(*) FROM lineage_retirements").fetchone()[0] <= 101
        assert c.execute("SELECT 1 FROM lineage_propagation WHERE done=0").fetchone()
        assert not c.execute("SELECT 1 FROM lineage_retirements WHERE fact_id='tail-child'").fetchone()
    assert lineage.unsupported_fact_ids(["tail-child"], as_of=99) == set()
    assert lineage.unsupported_fact_ids(["tail-child"], as_of=100) == {"tail-child"}


def test_deletion_intent_survives_post_primary_retirement_failure_and_recovers_owner(monkeypatch):
    from llm_mem0 import lineage
    memory = Memory([fact("source"), fact("derived", derived_from="source")])
    lineage.prepare_source_delete("source", "u")
    memory.delete("source")
    monkeypatch.setattr(lineage, "_stores", {})
    result = lineage.repair_dependents_sync(memory)
    assert result["pending"] == result["failed"] == 0
    assert memory.rows["derived"]["metadata"]["derived_status"] == "unsupported"


def test_planned_delete_does_not_retire_present_or_unconfirmed_source():
    from llm_mem0 import lineage
    memory = Memory([fact("present")])
    lineage.prepare_source_delete("present", "u")
    assert lineage.resolve_source_deletes_sync(memory)["pending"] == 0
    assert lineage.unsupported_fact_ids(["present"]) == set()
    lineage.prepare_source_delete("unknown", "u")
    memory.get = lambda fid: (_ for _ in ()).throw(TimeoutError("unconfirmed"))
    result = lineage.resolve_source_deletes_sync(memory)
    assert result["failed"] == result["pending"] == 1
    assert lineage.unsupported_fact_ids(["unknown"]) == set()


def test_dependent_owner_mismatch_remains_pending_without_mutation():
    from llm_mem0 import lineage
    wrong_owner = fact("derived")
    wrong_owner["user_id"] = "other"
    memory = Memory([wrong_owner])
    lineage.register_derivation("derived", "u", ["source"])
    lineage.retire_source("source")
    assert lineage.repair_dependents_sync(memory)["failed"] == 1
    assert memory.rows["derived"] == wrong_owner


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [{"status": "ok", "results": "bad"}, {"results": ["bad"]}])
async def test_malformed_ingest_result_never_advances_replay(monkeypatch, outcome):
    from llm_mem0 import history_index as h, ingest, replay
    history_user()
    monkeypatch.setattr(ingest, "add_memories", AsyncMock(return_value=outcome))
    assert (await replay.replay_channel_history(7, "u", dry_run=False))["failed"] == 1
    assert h.get_replay_cursor(7, "u") == ("", 0)


@pytest.mark.asyncio
async def test_delete_intent_directory_sync_failure_prevents_primary_delete(monkeypatch):
    from llm_mem0 import client, lineage
    memory = Memory([fact("source")])
    use_memory(monkeypatch, memory)
    def fail_sync():
        # The intent transaction is already visible before its directory sync.
        with lineage._store().connect() as c:
            assert c.execute("SELECT user_id FROM lineage_delete_intents WHERE source_id='source'").fetchone()[0] == "u"
        raise OSError("synthetic directory fsync failure")
    monkeypatch.setattr(lineage, "_sync_state_directory", fail_sync)
    assert not await client.delete_memory("source")
    assert "source" in memory.rows
    assert lineage.has_pending_repairs()


def test_pending_ancestor_depth_budget_is_fail_closed():
    from llm_mem0 import lineage
    for i in range(70):
        lineage.register_derivation(f"d-{i}", "u", [f"d-{i + 1}"])
    lineage.retire_source("unrelated", propagation_limit=0)
    assert lineage.unsupported_fact_ids(["d-0"]) == {"d-0"}

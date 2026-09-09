"""Independent reviewer regressions for ingest failure/retry and source edges."""
import asyncio
from copy import deepcopy
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(scope="module", autouse=True)
def alpha_bootstrap():
    import services.mem0 as package
    return package


class Memory:
    def __init__(self):
        self.rows = {}
        self.calls = 0

    def add(self, messages, *, user_id, metadata, infer=False):
        self.calls += 1
        fid = "new-" + str(self.calls)
        self.rows[fid] = {"id": fid, "user_id": user_id, "memory": messages[0]["content"], "metadata": deepcopy(metadata)}
        return {"results": [{"id": fid}]}

    def get(self, fid):
        return deepcopy(self.rows.get(fid))

    def get_all(self, *, user_id=None, filters=None, limit=None):
        uid = user_id or (filters or {}).get("user_id")
        return {"results": [deepcopy(row) for row in list(self.rows.values()) if row["user_id"] == uid]}

    def update(self, *, memory_id, data, metadata):
        self.rows[memory_id].update(memory=data, metadata=deepcopy(metadata))


def fact(text="体重58kg", **extra):
    return {"text": text, "speaker": "self", "importance": 4, "attribute": "weight",
            "scope": "global", "source_quote": text, **extra}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from llm_mem0 import ingest, search, graph, extract
    monkeypatch.setenv("LLM_MEM0_STATE_DIR", str(tmp_path))
    for key, value in {"MEM0_COLLECTION_NAME": "ingestion-review", "MEM0_GATE_ENABLED": "1",
                       "MEM0_INGEST_HASH_DEDUP": "1", "MEM0_CLIENT_DEDUP_ENABLED": "1",
                       "MEM0_DEDUP_TWO_STAGE": "1", "MEM0_CONFLICT_DETECTION_ENABLED": "1",
                       "MEM0_ATTRIBUTE_CONFLICT_ENABLED": "0", "MEM0_RELATION_EXTRACT_ENABLED": "0",
                       "MEM0_FACT_STORE_ENABLED": "0", "MEM0_DEDUP_DRY_RUN": "0",
                       "MEM0_ATTRIBUTE_ENABLED": "1", "MEM0_SCOPE_ENABLED": "1", "MEM0_CONDITION_ENABLED": "1",
                       "MEM0_EXTRACT_MAX_ATTEMPTS": "2", "MEM0_EXTRACT_INPUT_CHARS": "2000"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ingest, "_INSERT_TASKS", {})
    monkeypatch.setattr(search, "invalidate_core_cache", Mock())
    monkeypatch.setattr(extract, "_format_known_entities", lambda *a: "")
    from llm_mem0.sqlite_store import SqliteStore
    store = SqliteStore("graph.sqlite", graph._SCHEMA)
    store.reset_path_for_test(str(tmp_path / "graph.sqlite"))
    monkeypatch.setattr(graph, "_store", store)
    monkeypatch.setattr(graph, "_top_entities_cache", {})


@pytest.mark.parametrize("failed", ["graph_entity", "graph_edge", "bm25", "fact_store"])
@pytest.mark.asyncio
async def test_false_projection_return_is_not_success(monkeypatch, failed):
    from llm_mem0 import bm25_index, fact_store, graph, ingest, priming
    monkeypatch.setenv("MEM0_FACT_STORE_ENABLED", "1")
    monkeypatch.setattr(graph, "upsert_entity", lambda *a, **kw: 0 if failed == "graph_entity" else 1)
    monkeypatch.setattr(graph, "add_edge", lambda *a, **kw: failed != "graph_edge")
    monkeypatch.setattr(bm25_index, "index_fact", lambda *a, **kw: failed != "bm25")
    monkeypatch.setattr(fact_store, "record_fact", lambda *a, **kw: failed != "fact_store")
    monkeypatch.setattr(priming, "touch", lambda *a, **kw: None)
    errors = await ingest._index_side_stores("u", fact(entities=[{"name": "Alice", "type": "person"}]), {}, {}, ["f"])
    assert errors, "False/0 from side-store APIs must make the operation retryable"


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.asyncio
async def test_duplicate_missing_primary_or_failed_reinforcement_is_not_success(monkeypatch, exists):
    from llm_mem0 import ingest, reinforce
    mem = Memory()
    if exists:
        mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "体重58kg", "metadata": {"request_id": "origin"}}
    monkeypatch.setattr(ingest, "_obtain_signals", AsyncMock(return_value={"facts": [fact()], "status": "ok"}))
    monkeypatch.setattr(ingest, "_dedup_candidates", AsyncMock(return_value=([], 1, ["old"], [])))
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    monkeypatch.setattr(reinforce, "reinforce_existing_sync", Mock(return_value=False))
    result = await ingest._add_via_gate(mem, "u", "", "", "", {"request_id": "new-event"}, None)
    assert result["status"] == "error" and result["retryable"]


@pytest.mark.asyncio
async def test_extractor_missing_facts_field_is_not_successful_empty(monkeypatch):
    from llm_mem0 import extract
    backend = NS(complete=AsyncMock(return_value="{}"))
    monkeypatch.setattr(extract, "get_auth_backend", lambda: backend)
    result = await extract.extract_full_signals("体重58kg", "")
    assert result["status"] == "error" and result["retryable"]


@pytest.mark.asyncio
async def test_extraction_retry_does_not_discard_grounded_sibling(monkeypatch):
    from llm_mem0 import extract
    backend = NS(complete=AsyncMock(side_effect=[
        json.dumps({"facts": [fact(), fact("broken", importance="many")]}),
        json.dumps({"facts": []}),
    ]))
    monkeypatch.setattr(extract, "get_auth_backend", lambda: backend)
    result = await extract.extract_full_signals("体重58kg", "")
    assert [f["text"] for f in result["facts"]] == ["体重58kg"]
    assert result["status"] == "partial"


@pytest.mark.asyncio
async def test_parallel_distinct_events_do_not_insert_identical_fact_twice(monkeypatch):
    from llm_mem0 import ingest
    mem = Memory()
    monkeypatch.setattr(ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    async def near(*a, **kw):
        snapshot = [{**row, "score": .1} for row in mem.get_all(user_id="u")["results"]]
        await asyncio.sleep(.01)  # overlap two observations before either write
        return snapshot
    monkeypatch.setattr(ingest, "search_memories", near)
    results = await asyncio.gather(*(
        ingest.add_memories(user_id="u", metadata={"request_id": f"event-{n}"}, pre_extracted_facts=[fact()])
        for n in range(2)))
    assert all(result["status"] == "ok" for result in results), results
    assert len(mem.rows) == 1


@pytest.mark.parametrize("observations", [[(200, "体重58kg"), (300, "体重59kg")], [(300, "体重59kg"), (200, "体重58kg")]])
@pytest.mark.asyncio
async def test_parallel_attribute_corrections_keep_latest_observation_active(monkeypatch, observations):
    from llm_mem0 import conflict, ingest
    mem = Memory()
    mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "体重60kg", "metadata": {"attribute": "weight", "source_timestamp": 100}}
    monkeypatch.setattr(ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    async def near(*a, **kw):
        snapshot = [{**row, "score": .1} for row in mem.get_all(user_id="u")["results"] if not row["metadata"].get("valid_to")]
        await asyncio.sleep(.01)
        return snapshot
    async def classify(*a, **kw):
        await asyncio.sleep(.01)
        return {"conflict_type": "update"}
    monkeypatch.setattr(ingest, "search_memories", near)
    monkeypatch.setattr(conflict, "classify_conflict", classify)
    results = await asyncio.gather(*(
        ingest.add_memories(user_id="u", metadata={"request_id": f"event-{ts}", "source_timestamp": ts},
                            pre_extracted_facts=[fact(text)])
        for ts, text in observations))
    assert all(result["status"] == "ok" for result in results)
    active = [row["memory"] for row in mem.rows.values() if not row["metadata"].get("valid_to")]
    assert active == ["体重59kg"]


def test_expire_relations_for_source_is_idempotent_and_local_to_source():
    from llm_mem0 import graph
    src = graph.upsert_entity("Alice", "person", "u")
    first = graph.upsert_entity("First", "org", "u")
    second = graph.upsert_entity("Second", "org", "u")
    graph.add_relation(src, "works_at", first, "u", source_fact_id="old")
    graph.add_relation(src, "works_at", second, "u", source_fact_id="other")
    assert graph.expire_relations_for_fact("old", valid_to=100)
    assert graph.expire_relations_for_fact("old", valid_to=200)
    assert graph.expire_relations_for_fact("missing", valid_to=100)
    rows = {r.source_fact_id: r for r in graph.get_relations(src, only_valid=False)}
    assert rows["old"].valid_to == 100 and rows["other"].valid_to is None


def test_expire_relations_write_failure_is_not_success(monkeypatch):
    from llm_mem0 import graph
    monkeypatch.setattr(graph._store, "write", lambda *a, **kw: False)
    assert not graph.expire_relations_for_fact("fact")


@pytest.mark.parametrize("dedup", ["0", "1"])
@pytest.mark.asyncio
async def test_distinct_event_exact_receipts_do_not_depend_on_vector_freshness(monkeypatch, dedup):
    from llm_mem0 import ingest
    mem = Memory()
    monkeypatch.setenv("MEM0_CLIENT_DEDUP_ENABLED", dedup)
    monkeypatch.setattr(ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    monkeypatch.setattr(ingest, "search_memories", AsyncMock(return_value=[]))
    for event in ["one", "two", "two"]:
        result = await ingest.add_memories(user_id="u", metadata={"request_id": event}, pre_extracted_facts=[fact()])
        assert result["status"] == "ok"
    assert mem.calls == 1
    assert next(iter(mem.rows.values()))["metadata"]["mention_count"] == 2


@pytest.mark.asyncio
async def test_reused_source_receipt_cannot_resurrect_deleted_primary(monkeypatch):
    from llm_mem0 import ingest
    mem = Memory()
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    await ingest._insert_one(mem, "u", fact(), {"request_id": "one"})
    await ingest._insert_one(mem, "u", fact(), {"request_id": "two"})
    mem.rows.clear()
    result = await ingest._insert_one(mem, "u", fact(), {"request_id": "two"})
    assert result["error"] == "receipt_target_missing" and mem.calls == 1


@pytest.mark.parametrize("scope,condition,expected", [("global", "", 1), ("conversation:1", "", 0), ("global", "night", 0)])
@pytest.mark.asyncio
async def test_relation_projection_has_primary_source_and_does_not_broaden_scope(monkeypatch, scope, condition, expected):
    from llm_mem0 import ingest, graph
    mem = Memory()
    monkeypatch.setenv("MEM0_RELATION_EXTRACT_ENABLED", "1")
    monkeypatch.setenv("MEM0_CLIENT_DEDUP_ENABLED", "0")
    monkeypatch.setattr(ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    src = graph.upsert_entity("Alice", "person", "u")
    graph.upsert_entity("Acme", "org", "u")
    text = "Alice works at Acme"
    monkeypatch.setattr(ingest, "_obtain_signals", AsyncMock(return_value={
        "facts": [fact(text, scope=scope, condition=condition)],
        "relations": [{"src": "Alice", "rel_type": "works_at", "dst": "Acme", "source_quote": text}],
    }))
    result = await ingest._add_via_gate(mem, "u", text, "", "", {"request_id": "one", "valid_from": 100}, None)
    assert result["status"] == "ok"
    rows = graph.get_relations(src)
    assert len(rows) == expected
    if rows:
        assert rows[0].source_fact_id == "new-1" and rows[0].valid_from == 100


@pytest.mark.asyncio
async def test_archive_expires_supported_relation_and_retry_does_not_revive_it(monkeypatch):
    from llm_mem0 import ingest, graph
    mem = Memory()
    mem.rows["old"] = {"id": "old", "memory": "old", "user_id": "u", "metadata": {"attribute": "weight"}}
    src = graph.upsert_entity("Alice", "person", "u")
    dst = graph.upsert_entity("Acme", "org", "u")
    graph.add_relation(src, "works_at", dst, "u", source_fact_id="old")
    assert await ingest._archive_outdated_facts(mem, [("old", "update")], user_id="u", new_fact_id="new",
                                               candidate_metadata={"attribute": "weight"}) == 1
    assert graph.get_relations(src) == []
    assert await ingest._archive_outdated_facts(mem, [("old", "update")], user_id="u", new_fact_id="different",
                                               candidate_metadata={"attribute": "weight"}) == 0
    assert mem.rows["old"]["metadata"]["superseded_by"] == "new"


def test_closed_relation_projection_redelivery_is_idempotent():
    from llm_mem0 import graph
    src = graph.upsert_entity("Alice", "person", "u")
    dst = graph.upsert_entity("Acme", "org", "u")
    a = graph.add_relation(src, "works_at", dst, "u", source_fact_id="old", valid_from=100, valid_to=200)
    b = graph.add_relation(src, "works_at", dst, "u", source_fact_id="old", valid_from=100, valid_to=200)
    assert a == b and a
    assert graph.get_relations(src) == []
    assert len(graph.get_relations(src, only_valid=False)) == 1


@pytest.mark.asyncio
async def test_relation_projection_failure_repaired_after_exact_duplicate(monkeypatch):
    from llm_mem0 import ingest
    mem = Memory()
    text = "体重58kg"
    monkeypatch.setenv("MEM0_RELATION_EXTRACT_ENABLED", "1")
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    monkeypatch.setattr(ingest, "_obtain_signals", AsyncMock(return_value={"facts": [fact()], "relations": [
        {"src": "A", "rel_type": "knows", "dst": "B", "source_quote": text}]}))
    relation = AsyncMock(side_effect=[0, 1])
    monkeypatch.setattr(ingest, "_ingest_relations", relation)
    async def near(*args, **kwargs):
        return [{**row, "score": .1} for row in mem.rows.values()]
    monkeypatch.setattr(ingest, "search_memories", near)
    first = await ingest._add_via_gate(mem, "u", text, "", "", {"request_id": "event"}, None)
    second = await ingest._add_via_gate(mem, "u", text, "", "", {"request_id": "event"}, None)
    assert first["status"] == "partial" and second["status"] == "ok"
    assert relation.await_count == 2 and mem.calls == 1


@pytest.mark.asyncio
async def test_cancelled_ingest_keeps_domain_owned_until_commit(monkeypatch):
    import threading
    from llm_mem0 import ingest
    entered, release = threading.Event(), threading.Event()
    mem = Memory()
    original = mem.add
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    mem.add = slow
    monkeypatch.setattr(ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    near = AsyncMock(return_value=[])
    monkeypatch.setattr(ingest, "search_memories", near)
    first = asyncio.create_task(ingest.add_memories(user_id="u", metadata={"request_id": "one"}, pre_extracted_facts=[fact()]))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(ingest.add_memories(user_id="u", metadata={"request_id": "two"}, pre_extracted_facts=[fact()]))
        await asyncio.sleep(.04)
        assert near.await_count == 1 and not second.done()
    finally:
        release.set()
    assert (await second)["status"] == "ok" and mem.calls == 1


@pytest.mark.asyncio
async def test_domain_lock_coordinates_independent_process(tmp_path):
    import subprocess
    import sys
    from llm_mem0 import ingest
    async with ingest._ingestion_domains("u", [fact()], {}):
        paths = list((tmp_path / "ingest_domain_locks").glob("*.lock"))
    assert len(paths) == 1
    script = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()"
    child = subprocess.Popen([sys.executable, "-c", script, str(paths[0])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert await asyncio.to_thread(child.stdout.readline) == "locked\n"
        entered = asyncio.Event()
        async def acquire():
            async with ingest._ingestion_domains("u", [fact()], {}):
                entered.set()
        task = asyncio.create_task(acquire())
        await asyncio.sleep(.04)
        assert not entered.is_set()
        child.stdin.write("release\n")
        child.stdin.flush()
        await asyncio.wait_for(task, 2)
        assert entered.is_set()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


def test_asyncio_run_shutdown_keeps_domain_locked_until_provider_finishes(monkeypatch, tmp_path):
    import fcntl
    import threading
    import time
    from llm_mem0 import ingest
    mem = Memory()
    entered, release = threading.Event(), threading.Event()
    observed = []
    original = mem.add
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    mem.add = slow
    monkeypatch.setattr(ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(ingest, "search_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    def observe():
        try:
            assert entered.wait(2)
            time.sleep(.08)  # asyncio.run has cancelled every remaining Task
            paths = list((tmp_path / "ingest_domain_locks").glob("*.lock"))
            for path in paths:
                with path.open("a") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        observed.append("released-before-commit")
                    except BlockingIOError:
                        observed.append("held-until-commit")
        finally:
            release.set()
    observer = threading.Thread(target=observe)
    observer.start()
    async def main():
        asyncio.create_task(ingest.add_memories(user_id="u", metadata={"request_id": "shutdown"}, pre_extracted_facts=[fact()]))
        assert await asyncio.to_thread(entered.wait, 2)
    try:
        asyncio.run(main())
    finally:
        release.set()
        observer.join(timeout=3)
    assert observed == ["held-until-commit"] and mem.calls == 1


@pytest.mark.asyncio
async def test_archive_rejects_explicit_metadata_owner_mismatch():
    from llm_mem0 import ingest
    mem = Memory()
    mem.rows["old"] = {"memory": "other owner", "metadata": {"user_id": "other", "attribute": "weight"}}
    assert await ingest._archive_outdated_facts(mem, [("old", "update")], user_id="u", new_fact_id="new",
                                               candidate_metadata={"attribute": "weight"}) == 0
    assert "valid_to" not in mem.rows["old"]["metadata"]


@pytest.mark.asyncio
async def test_reaffirmation_time_protects_current_value_from_older_backfill(monkeypatch):
    from llm_mem0 import ingest, conflict
    mem = Memory()
    monkeypatch.setattr(ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    monkeypatch.setattr(conflict, "classify_conflict", AsyncMock(return_value={"conflict_type": "update"}))
    async def near(*args, **kwargs):
        return [{**row, "score": .1} for row in mem.rows.values() if not row["metadata"].get("valid_to")]
    monkeypatch.setattr(ingest, "search_memories", near)
    for ts, text in [(100, "体重58kg"), (300, "体重58kg"), (200, "体重59kg")]:
        result = await ingest.add_memories(user_id="u", metadata={"request_id": str(ts), "source_timestamp": ts},
                                          pre_extracted_facts=[fact(text)])
        assert result["status"] == "ok", result
    active = [row for row in mem.rows.values() if not row["metadata"].get("valid_to")]
    assert len(active) == 1 and active[0]["memory"] == "体重58kg"
    assert active[0]["metadata"]["source_timestamp"] == 100
    assert active[0]["metadata"]["user_confirmed_at"] == 300

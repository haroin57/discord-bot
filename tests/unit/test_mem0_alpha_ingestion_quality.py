"""Offline alpha ingestion contracts; no provider or production state access."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(scope="module", autouse=True)
def mem0_alpha_bootstrap():
    import services.mem0 as package
    return package


class Memory:
    def __init__(self):
        self.rows = {}
        self.calls = 0
        self.fail = 0

    def add(self, messages, *, user_id, metadata, infer):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise TimeoutError("fixture")
        fid = f"new-{self.calls}"
        self.rows[fid] = {"id": fid, "user_id": user_id, "memory": messages[0]["content"],
                          "metadata": deepcopy(metadata)}
        return {"results": [{"id": fid}]}

    def get(self, fid):
        return deepcopy(self.rows.get(fid))

    def get_all(self, *, user_id=None, filters=None, limit=None):
        user_id = user_id or (filters or {}).get("user_id")
        return {"results": [deepcopy(row) for row in self.rows.values() if row.get("user_id") == user_id]}

    def update(self, *, memory_id, data, metadata):
        self.rows[memory_id]["memory"] = data
        self.rows[memory_id]["metadata"] = deepcopy(metadata)


def fact(text="体重は58kg", **kwargs):
    return {"speaker": "self", "text": text, "importance": 4, "category": "personal",
            "attribute": "weight", "scope": "global", "source_quote": text, **kwargs}


@pytest.fixture
def modules(monkeypatch, tmp_path):
    from llm_mem0 import ingest, extract, dedup, conflict, reinforce, attribute_registry, search
    from llm_mem0 import graph
    from llm_mem0.sqlite_store import SqliteStore
    store = SqliteStore("graph.sqlite", graph._SCHEMA)
    store.reset_path_for_test(str(tmp_path / "graph.sqlite"))
    monkeypatch.setattr(graph, "_store", store)
    monkeypatch.setenv("LLM_MEM0_STATE_DIR", str(tmp_path))
    for key, value in {
        "MEM0_INGEST_HASH_DEDUP": "true", "MEM0_GATE_ENABLED": "true",
        "MEM0_ATTRIBUTE_CONFLICT_ENABLED": "false", "MEM0_SCOPE_ENABLED": "true",
        "MEM0_ATTRIBUTE_ENABLED": "true", "MEM0_CONDITION_ENABLED": "true",
        "MEM0_FACT_STORE_ENABLED": "false", "MEM0_DEDUP_DRY_RUN": "false",
        "MEM0_DEDUP_TWO_STAGE": "true", "MEM0_RELATION_EXTRACT_ENABLED": "false",
        "MEM0_EXTRACT_MAX_ATTEMPTS": "2", "MEM0_EXTRACT_MAX_CHUNKS": "16",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ingest, "_INSERT_TASKS", {})
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock())
    monkeypatch.setattr(extract, "_format_known_entities", lambda *_: "")
    monkeypatch.setattr(search, "invalidate_core_cache", Mock())
    return NS(ingest=ingest, extract=extract, dedup=dedup, conflict=conflict,
              reinforce=reinforce, registry=attribute_registry, search=search)


@pytest.mark.asyncio
async def test_failed_insert_never_archives_predecessor(modules, monkeypatch):
    i = modules.ingest
    f = fact(_predecessors=[("old", "update")])
    monkeypatch.setattr(i, "_obtain_signals", AsyncMock(return_value={"facts": [f]}))
    monkeypatch.setattr(i, "_dedup_candidates", AsyncMock(return_value=([f], 0, [], [("old", "update")])))
    monkeypatch.setattr(i, "_insert_one", AsyncMock(return_value=None))
    archive = AsyncMock()
    monkeypatch.setattr(i, "_archive_outdated_facts", archive)
    result = await i._add_via_gate(Memory(), "u", "", "", "", {}, None)
    assert result["status"] == "error" and result["stored"] == 0 and result["failed"] == 1
    archive.assert_not_called()


@pytest.mark.asyncio
async def test_partial_insert_archives_only_matching_candidate(modules, monkeypatch):
    i = modules.ingest
    a, b = fact("A", _predecessors=[("old-a", "update")]), fact("B", _predecessors=[("old-b", "update")])
    monkeypatch.setattr(i, "_obtain_signals", AsyncMock(return_value={"facts": [a, b]}))
    monkeypatch.setattr(i, "_dedup_candidates", AsyncMock(return_value=([a, b], 0, [], [])))
    monkeypatch.setattr(i, "_insert_one", AsyncMock(side_effect=[{"results": [{"id": "new-a"}]}, None]))
    archive = AsyncMock(return_value=1)
    monkeypatch.setattr(i, "_archive_outdated_facts", archive)
    result = await i._add_via_gate(Memory(), "u", "", "", "", {}, None)
    assert result["status"] == "partial" and result["stored"] == 1
    assert archive.await_args.args[1] == [("old-a", "update")]
    assert archive.await_args.kwargs["new_fact_id"] == "new-a"


@pytest.mark.asyncio
async def test_insert_failure_can_retry_and_receipt_survives_task_reset(modules):
    mem = Memory()
    mem.fail = 1
    first = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "event"})
    assert first["error"]
    second = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "event"})
    modules.ingest._INSERT_TASKS.clear()
    third = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "event"})
    assert second == third and second["results"][0]["id"] == "new-2"
    assert mem.calls == 2


@pytest.mark.asyncio
async def test_unconfirmed_sdk_result_is_retryable(modules):
    mem = NS(add=Mock(return_value={"results": []}), get_all=Mock(return_value={"results": []}))
    for _ in range(2):
        result = await modules.ingest._insert_one(mem, "u", fact(), {})
        assert result["error"] == "insert_unconfirmed"
    assert mem.add.call_count == 2


@pytest.mark.asyncio
async def test_scope_and_condition_are_part_of_insert_identity(modules):
    mem = Memory()
    for scope, condition in [("conversation:1", ""), ("conversation:2", ""), ("conversation:1", "time:night")]:
        await modules.ingest._insert_one(mem, "u", fact(scope=scope, condition=condition), {"request_id": "same"})
    assert mem.calls == 3


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_live_insert(modules):
    mem = Memory()
    entered, release = threading.Event(), threading.Event()
    original = mem.add
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    mem.add = slow
    task = asyncio.create_task(modules.ingest._insert_one(mem, "u", fact(), {"request_id": "event"}))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    retry = asyncio.create_task(modules.ingest._insert_one(mem, "u", fact(), {"request_id": "event"}))
    await asyncio.sleep(0)
    release.set()
    assert (await retry)["results"][0]["id"] == "new-1"
    assert mem.calls == 1


@pytest.mark.asyncio
async def test_archive_preserves_metadata_and_records_successor(modules):
    mem = Memory()
    mem.rows["old"] = {"memory": "old text", "user_id": "u", "metadata": {
        "attribute": "weight", "importance": 5, "scope": "conversation:1", "source_timestamp": 100}}
    count = await modules.ingest._archive_outdated_facts(mem, [("old", "update")], user_id="u", new_fact_id="new",
        candidate_metadata={"attribute": "weight", "scope": "conversation:1", "source_timestamp": 200})
    assert count == 1
    assert mem.rows["old"]["metadata"]["importance"] == 5
    assert mem.rows["old"]["metadata"]["superseded_by"] == "new"
    assert mem.rows["old"]["memory"] == "old text"


@pytest.mark.asyncio
async def test_near_numeric_correction_is_never_hard_dropped(modules, monkeypatch):
    judge = AsyncMock(return_value="paraphrase")
    monkeypatch.setattr(modules.dedup, "judge_fact_relation", judge)
    monkeypatch.setattr(modules.conflict, "classify_conflict", AsyncMock(return_value={"conflict_type": "update"}))
    result = await modules.dedup.check_near_dup("体重58kg", "personal", [
        {"id": "old", "memory": "体重60kg", "score": .1, "metadata": {"attribute": "weight"}}],
        detect_conflict=True, candidate_metadata={"attribute": "weight"})
    assert result[0] is False and result[3] == "update"
    judge.assert_not_called()


@pytest.mark.asyncio
async def test_exact_duplicate_precedes_earlier_merge_neighbor(modules, monkeypatch):
    judge = AsyncMock()
    monkeypatch.setattr(modules.dedup, "judge_fact_relation", judge)
    result = await modules.dedup.check_near_dup("same", None, [
        {"id": "merge", "memory": "similar", "score": .1},
        {"id": "exact", "memory": "same", "score": .2}])
    assert result[:3] == (True, "exact", "exact")
    judge.assert_not_called()


@pytest.mark.parametrize("old,new", [
    ({"scope": "conversation:1"}, {"scope": "conversation:2"}),
    ({"condition": "time:morning"}, {"condition": "time:night"}),
    ({"condition": ""}, {"condition": "time:morning"}),
    ({"source_timestamp": 200}, {"source_timestamp": 100}),
    ({"source_timestamp": 200}, {}),
    ({"timestamp": 200}, {"source_timestamp": 100}),
    ({"attribute": "preference.music"}, {"attribute": "preference.music"}),
])
def test_conflict_domain_rejects_destructive_mismatch(modules, old, new):
    assert not modules.registry.can_supersede({"attribute": "weight", **old}, {"attribute": "weight", **new})


@pytest.mark.asyncio
async def test_attribute_conflict_respects_scope(modules, monkeypatch):
    monkeypatch.setattr(modules.ingest, "_get_facts_by_attribute", AsyncMock(return_value=[
        {"id": "old", "memory": "old", "metadata": {"attribute": "weight", "scope": "conversation:1"}}]))
    classify = AsyncMock(return_value={"conflict_type": "update"})
    monkeypatch.setattr(modules.conflict, "classify_conflict", classify)
    result = await modules.ingest._attribute_conflict_check(Memory(), "u", [fact(scope="conversation:2")], exclude_ids=set())
    assert result == []
    classify.assert_not_called()


@pytest.mark.asyncio
async def test_nontransitive_merge_never_fuses_different_pair(modules, monkeypatch):
    monkeypatch.setattr(modules.dedup, "judge_fact_relation", AsyncMock(side_effect=["mergeable", "different", "mergeable"]))
    groups = await modules.dedup.partition_mergeable(["A", "A+B", "B"])
    assert not any(0 in group and 2 in group for group in groups)
    assert sorted(member for group in groups for member in group) == [0, 1, 2]


def backend_for(monkeypatch, modules, payloads):
    backend = NS(complete=AsyncMock(side_effect=[json.dumps(p) if isinstance(p, dict) else p for p in payloads]))
    monkeypatch.setattr(modules.extract, "get_auth_backend", lambda: backend)
    return backend


@pytest.mark.asyncio
async def test_bad_relation_does_not_erase_good_fact(modules, monkeypatch):
    backend_for(monkeypatch, modules, [{"facts": [fact()], "relations": [{"src": 42, "dst": "B", "rel_type": "knows"}]}])
    result = await modules.extract.extract_full_signals("体重は58kg", "")
    assert len(result["facts"]) == 1 and result["relations"] == []
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_transient_extraction_failure_retries(modules, monkeypatch):
    backend = backend_for(monkeypatch, modules, [TimeoutError("fixture"), {"facts": [fact()]}])
    result = await modules.extract.extract_full_signals("体重は58kg", "")
    assert backend.complete.await_count == 2 and len(result["facts"]) == 1
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_persistent_extraction_failure_is_not_empty_success(modules, monkeypatch):
    backend_for(monkeypatch, modules, [TimeoutError("fixture"), TimeoutError("fixture")])
    result = await modules.extract.extract_full_signals("text", "")
    assert result["status"] == "error" and result["retryable"] and result["error"]


@pytest.mark.asyncio
async def test_assistant_only_claim_is_not_attributed_to_user(modules, monkeypatch):
    payload = {"facts": [fact("大阪に住む", source_quote="大阪に住む")]}
    backend_for(monkeypatch, modules, [payload, payload])
    result = await modules.extract.extract_full_signals("こんにちは", "大阪に住む")
    assert result["facts"] == [] and result["status"] == "error"


@pytest.mark.asyncio
async def test_invalid_fact_does_not_discard_valid_sibling(modules, monkeypatch):
    payload = {"facts": [fact(), fact("bad", importance="many")]}
    backend_for(monkeypatch, modules, [payload, payload])
    result = await modules.extract.extract_full_signals("体重は58kg", "")
    assert len(result["facts"]) == 1 and result["status"] == "partial"


@pytest.mark.asyncio
async def test_long_input_extracts_tail_with_bounded_concurrency(modules, monkeypatch):
    monkeypatch.setenv("MEM0_EXTRACT_INPUT_CHARS", "128")
    active = maximum = calls = 0
    async def complete(**kwargs):
        nonlocal active, maximum, calls
        active += 1
        maximum = max(maximum, active)
        calls += 1
        await asyncio.sleep(.002)
        active -= 1
        return json.dumps({"facts": [fact("TAIL")] if "TAIL" in kwargs["user_message"] else []})
    monkeypatch.setattr(modules.extract, "get_auth_backend", lambda: NS(complete=complete))
    text = "前" * 500 + "TAIL"
    result = await modules.extract.extract_full_signals(text, "")
    assert [f["text"] for f in result["facts"]] == ["TAIL"]
    assert result["covered_chars"] == len(text) and result["status"] == "ok"
    assert 1 < maximum <= 2 and 1 < calls <= 16


@pytest.mark.asyncio
async def test_chunk_budget_overflow_is_explicit(modules, monkeypatch):
    monkeypatch.setenv("MEM0_EXTRACT_INPUT_CHARS", "128")
    monkeypatch.setenv("MEM0_EXTRACT_MAX_CHUNKS", "1")
    backend_for(monkeypatch, modules, [{"facts": []}])
    result = await modules.extract.extract_full_signals("x" * 1000, "")
    assert result["status"] == "error" and "input_exceeds_chunk_budget" in result["error"]


def test_all_user_messages_and_provenance_are_preserved(modules):
    user, assistant, _ = modules.ingest._normalize_signature([
        {"role": "user", "content": "first"}, {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second correction"}], None, None, "")
    assert "first" in user and "second correction" in user and assistant == "reply"
    md = modules.ingest._build_base_meta({"request_id": "r", "source_author_id": "u", "source_message_ids": ["1", "2"],
        "source_timestamp": 100, "batch_id": "b", "batch_size": 2, "importance": 100})
    assert json.loads(md["source_message_ids"]) == ["1", "2"] and md["source_timestamp"] == 100
    assert "importance" not in md


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_typed_values_reject_nonfinite_and_boolean(modules, value):
    assert modules.extract._normalize_value({"type": "quantity", "value": value, "unit": "kg"}) is None


def test_eight_concurrent_reinforcements_preserve_all_updates(modules):
    mem = Memory()
    mem.rows["fact"] = {"memory": "text", "metadata": {"mention_count": 1, "importance": 5, "scope": "conversation:1"}}
    original = mem.get
    def slow_get(fid):
        result = original(fid)
        time.sleep(.002)
        return result
    mem.get = slow_get
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: modules.reinforce.reinforce_existing_sync(mem, "fact"), range(8)))
    assert all(results) and mem.rows["fact"]["metadata"]["mention_count"] == 9
    assert mem.rows["fact"]["metadata"]["importance"] == 5
    assert mem.rows["fact"]["metadata"]["scope"] == "conversation:1"


def test_reinforcement_redelivery_and_recall_spacing(modules):
    mem = Memory()
    mem.rows["fact"] = {"memory": "text", "metadata": {"mention_count": 1, "request_id": "origin"}}
    assert not modules.reinforce.reinforce_existing_sync(mem, "fact", event_id="origin")
    assert modules.reinforce.reinforce_existing_sync(mem, "fact", event_id="new")
    assert not modules.reinforce.reinforce_existing_sync(mem, "fact", event_id="new")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: modules.reinforce.reinforce_on_recall_sync(mem, "fact"), range(8)))
    assert sum(results) == 1 and mem.rows["fact"]["metadata"]["recall_count"] == 1


@pytest.mark.asyncio
async def test_store_initialization_error_is_reported(modules, monkeypatch):
    monkeypatch.setattr(modules.ingest, "_get_mem0", Mock(side_effect=TimeoutError("fixture")))
    result = await modules.ingest.add_memories(user_id="u", pre_extracted_facts=[fact()])
    assert result["status"] == "error" and result["retryable"]


@pytest.mark.asyncio
async def test_deleted_receipt_target_is_not_recreated_or_confirmed(modules):
    mem = Memory()
    original = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "r"})
    mem.rows.pop(original["results"][0]["id"])
    result = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "r"})
    assert result["error"] == "receipt_target_missing" and mem.calls == 1


@pytest.mark.asyncio
async def test_receipt_target_owner_is_verified(modules):
    mem = Memory()
    original = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "r"})
    mem.rows[original["results"][0]["id"]]["user_id"] = "different-user"
    result = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "r"})
    assert result["error"] == "receipt_target_mismatch" and mem.calls == 1


@pytest.mark.asyncio
async def test_receipt_namespace_includes_collection_and_endpoint(modules, monkeypatch):
    first, second, third = Memory(), Memory(), Memory()
    await modules.ingest._insert_one(first, "u", fact(), {"request_id": "r"})
    monkeypatch.setenv("MEM0_COLLECTION_NAME", "different-collection")
    result = await modules.ingest._insert_one(second, "u", fact(), {"request_id": "r"})
    assert result["results"] and second.calls == 1
    monkeypatch.setenv("CHROMA_PORT", "9999")
    result = await modules.ingest._insert_one(third, "u", fact(), {"request_id": "r"})
    assert result["results"] and third.calls == 1


@pytest.mark.asyncio
async def test_timeout_after_primary_commit_is_reconciled(modules):
    mem = Memory()
    original = mem.add
    def commit_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("response lost after commit")
    mem.add = commit_then_timeout
    failed = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "r"})
    assert failed["error"]
    mem.add = original
    recovered = await modules.ingest._insert_one(mem, "u", fact(), {"request_id": "r"})
    assert recovered["results"][0]["id"] == "new-1" and mem.calls == 1


@pytest.mark.asyncio
async def test_same_batch_updates_are_ordered_and_only_latest_is_active(modules, monkeypatch):
    mem = Memory()
    monkeypatch.setattr(modules.ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(modules.ingest, "search_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(modules.conflict, "classify_conflict", AsyncMock(return_value={"conflict_type": "update"}))
    monkeypatch.setenv("MEM0_CONFLICT_DETECTION_ENABLED", "true")
    result = await modules.ingest.add_memories(user_id="u", metadata={"batch_id": "r"}, pre_extracted_facts=[
        fact("大阪に住む", attribute="residence", source_offset=20),
        fact("東京に住む", attribute="residence", source_offset=0)])
    assert result["status"] == "ok" and result["stored"] == 2
    active = [row["memory"] for row in mem.rows.values() if not row["metadata"].get("valid_to")]
    assert active == ["大阪に住む"]
    assert mem.rows["new-1"]["metadata"]["superseded_by"] == "new-2"


@pytest.mark.asyncio
async def test_pending_archive_is_repaired_after_duplicate_redelivery(modules, monkeypatch):
    mem = Memory()
    mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "old", "metadata": {"attribute": "weight"}}
    mem.rows["replacement"] = {"id": "replacement", "user_id": "u", "memory": "new", "metadata": {
        "attribute": "weight", "pending_predecessors": json.dumps([["old", "update"]])}}
    monkeypatch.setattr(modules.ingest, "_obtain_signals", AsyncMock(return_value={"facts": [fact()]}))
    monkeypatch.setattr(modules.ingest, "_dedup_candidates", AsyncMock(return_value=([], 1, ["replacement"], [])))
    result = await modules.ingest._add_via_gate(mem, "u", "", "", "", {}, None)
    assert result["status"] == "ok" and mem.rows["old"]["metadata"]["superseded_by"] == "replacement"


@pytest.mark.asyncio
async def test_projection_error_is_partial_and_replayed(modules, monkeypatch):
    mem = Memory()
    monkeypatch.setattr(modules.ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(modules.ingest, "search_memories", AsyncMock(return_value=[]))
    project = AsyncMock(side_effect=[["bm25"], []])
    monkeypatch.setattr(modules.ingest, "_index_side_stores", project)
    first = await modules.ingest.add_memories(user_id="u", metadata={"request_id": "r"}, pre_extracted_facts=[fact()])
    assert first["status"] == "partial" and first["stored"] == 1
    second = await modules.ingest.add_memories(user_id="u", metadata={"request_id": "r"}, pre_extracted_facts=[fact()])
    assert second["status"] == "ok" and mem.calls == 1 and project.await_count == 2


@pytest.mark.asyncio
async def test_explicit_interval_and_observation_time_are_stored_separately(modules, monkeypatch):
    mem = Memory()
    monkeypatch.setattr(modules.ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(modules.ingest, "search_memories", AsyncMock(return_value=[]))
    result = await modules.ingest.add_memories(user_id="u", metadata={"source_timestamp": 500, "valid_from": 100, "valid_to": 200},
        pre_extracted_facts=[fact(source_timestamp=300)])
    assert result["status"] == "ok"
    md = mem.rows["new-1"]["metadata"]
    assert (md["source_timestamp"], md["valid_from"], md["valid_to"]) == (300, 100, 200)


@pytest.mark.parametrize("metadata", [
    {"valid_from": True}, {"valid_to": float("inf")}, {"valid_from": "100"},
    {"valid_from": 200, "valid_to": 100}, {"source_timestamp": False}, {"source_timestamp": float("nan")},
])
def test_invalid_programmatic_time_is_rejected(modules, metadata):
    with pytest.raises(ValueError):
        modules.ingest._build_base_meta(metadata)
    assert modules.extract.normalize_fact(fact(**metadata), programmatic=True) is None


@pytest.mark.asyncio
async def test_llm_cannot_invent_effective_interval(modules, monkeypatch):
    backend_for(monkeypatch, modules, [{"facts": [fact(valid_from=100, valid_to=200, source_timestamp=300)]}])
    result = await modules.extract.extract_full_signals("体重は58kg", "")
    assert len(result["facts"]) == 1
    assert not ({"valid_from", "valid_to", "source_timestamp"} & result["facts"][0].keys())

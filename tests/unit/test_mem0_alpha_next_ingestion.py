"""Second-stage ingestion regressions; fake models/providers and private state."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import asyncio
import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(scope="module", autouse=True)
def bootstrap():
    import services.mem0 as package
    return package


class Memory:
    def __init__(self):
        self.rows = {}
        self.calls = 0
        self.lookup_fails = False
        self.fail_add = 0
        self.update_timeout = False

    def add(self, messages, *, user_id, metadata, infer=False):
        self.calls += 1
        if self.fail_add:
            self.fail_add -= 1
            raise TimeoutError("synthetic insert failure")
        fid = "new-" + str(self.calls)
        self.rows[fid] = {"id": fid, "user_id": user_id, "memory": messages[0]["content"],
                          "metadata": deepcopy(metadata)}
        return {"results": [{"id": fid}]}

    def get(self, fid):
        return deepcopy(self.rows.get(fid))

    def get_all(self, *, user_id=None, filters=None, limit=None):
        if self.lookup_fails:
            raise TimeoutError("synthetic lookup failure")
        uid = user_id or (filters or {}).get("user_id")
        return {"results": [deepcopy(row) for row in self.rows.values() if row["user_id"] == uid]}

    def update(self, *, memory_id, data, metadata):
        self.rows[memory_id].update(memory=data, metadata=deepcopy(metadata))
        if self.update_timeout:
            self.update_timeout = False
            raise TimeoutError("synthetic response lost after commit")


def fact(text, **extra):
    return {"speaker": "self", "text": text, "importance": 4, "category": "personal",
            "attribute": "residence", "scope": "global", "source_quote": text, **extra}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    from llm_mem0 import conflict, dedup, extract, graph, ingest, ingest_journal, reinforce, search
    real_judge = dedup.judge_fact_relation
    real_classify = conflict.classify_conflict
    mem = Memory()
    for key, value in {
        "LLM_MEM0_STATE_DIR": str(tmp_path), "MEM0_COLLECTION_NAME": "next-ingestion",
        "MEM0_GATE_ENABLED": "1", "MEM0_INGEST_HASH_DEDUP": "1", "MEM0_CLIENT_DEDUP_ENABLED": "0",
        "MEM0_ATTRIBUTE_CONFLICT_ENABLED": "1", "MEM0_CONFLICT_DETECTION_ENABLED": "1",
        "MEM0_RELATION_EXTRACT_ENABLED": "0", "MEM0_FACT_STORE_ENABLED": "0",
        "MEM0_ATTRIBUTE_ENABLED": "1", "MEM0_SCOPE_ENABLED": "1", "MEM0_CONDITION_ENABLED": "1",
        "MEM0_DEDUP_TWO_STAGE": "1", "MEM0_DEDUP_DRY_RUN": "0",
        "MEM0_EXTRACT_INPUT_CHARS": "128", "MEM0_EXTRACT_MAX_CHUNKS": "2",
        "MEM0_EXTRACT_MAX_ATTEMPTS": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ingest, "_get_mem0", lambda: mem)
    monkeypatch.setattr(ingest, "search_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(ingest, "_index_side_stores", AsyncMock(return_value=[]))
    monkeypatch.setattr(search, "invalidate_core_cache", lambda *a, **kw: None)
    monkeypatch.setattr(graph, "expire_relations_for_fact", lambda *a, **kw: True)
    monkeypatch.setattr(extract, "_format_known_entities", lambda *a: "")
    monkeypatch.setattr(conflict, "classify_conflict", AsyncMock(return_value={"conflict_type": "update"}))
    monkeypatch.setattr(dedup, "judge_fact_relation", AsyncMock(return_value="different"))
    return SimpleNamespace(mem=mem, ingest=ingest, extract=extract, conflict=conflict, dedup=dedup,
                           reinforce=reinforce, journal=ingest_journal, real_judge=real_judge,
                           real_classify=real_classify)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("operation", []), ("operation", {}),
                                         ("scope", []), ("scope", {})])
async def test_malformed_item_does_not_discard_valid_extraction_sibling(setup, monkeypatch, field, value):
    s = setup
    text = "東京に住んでいます。"
    raw = json.dumps({"facts": [fact(text), fact(text, **{field: value})]}, ensure_ascii=False)
    monkeypatch.setattr(s.extract, "get_auth_backend", lambda: SimpleNamespace(complete=AsyncMock(return_value=raw)))
    result = await s.extract.extract_full_signals(text, "", user_id="u")
    assert result["status"] == "partial" and result["retryable"]
    assert [f["text"] for f in result["facts"]] == [text]
    assert "TypeError" not in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{}, {"conflict_type": None}, {"conflict_type": []},
                                  {"conflict_type": {}}, {"conflict_type": "unknown"}])
async def test_missing_or_malformed_classifier_type_is_retryable(setup, monkeypatch, data):
    s = setup
    monkeypatch.setattr(s.conflict, "complete_json", AsyncMock(return_value=data))
    metadata = {"attribute": "residence", "scope": "global"}
    result = await s.real_classify("東京に住む", "大阪に住む",
                                   existing_metadata=metadata, candidate_metadata=metadata)
    assert result["retryable"] and result["conflict_type"] == "addition"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("hash_dedup", ["0", "1"])
async def test_same_fact_retains_distinct_source_events_in_one_batch(setup, monkeypatch, existing, hash_dedup):
    s = setup
    text = "コーヒーを飲む習慣がある"
    monkeypatch.setenv("MEM0_CLIENT_DEDUP_ENABLED", "1")
    monkeypatch.setenv("MEM0_ATTRIBUTE_CONFLICT_ENABLED", "0")
    monkeypatch.setenv("MEM0_INGEST_HASH_DEDUP", hash_dedup)
    if existing:
        s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": text,
                             "metadata": {"attribute": "habit.coffee", "scope": "global", "mention_count": 1}}
    async def fresh(*args, **kwargs):
        return [{**s.mem.get(fid), "score": .1} for fid in s.mem.rows]
    monkeypatch.setattr(s.ingest, "search_memories", fresh)
    facts = [fact(text, attribute="habit.coffee", request_id=event, source_timestamp=stamp)
             for event, stamp in [("event-1", 100), ("event-2", 200), ("event-1", 100)]]
    kwargs = {"user_id": "u", "metadata": {"batch_id": "batch"}}
    result = await s.ingest.add_memories(**kwargs, pre_extracted_facts=facts)
    assert result["status"] == "ok", result
    fid = "old" if existing else "new-1"
    expected = 3 if existing else 2
    assert s.mem.rows[fid]["metadata"]["mention_count"] == expected
    assert s.mem.calls == (0 if existing else 1)
    assert s.journal.event_recorded(fid, "event-1")
    assert s.journal.event_recorded(fid, "event-2")
    for replay in (facts, facts[1:2]):
        assert (await s.ingest.add_memories(**kwargs, pre_extracted_facts=replay))["status"] == "ok"
        assert s.mem.rows[fid]["metadata"]["mention_count"] == expected


@pytest.mark.asyncio
async def test_second_confirmation_failure_is_not_acknowledged_and_retries_once(setup, monkeypatch):
    s = setup
    text = "コーヒーを飲む習慣がある"
    s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": text,
                         "metadata": {"attribute": "habit.coffee", "scope": "global", "mention_count": 1}}
    monkeypatch.setenv("MEM0_CLIENT_DEDUP_ENABLED", "1")
    monkeypatch.setenv("MEM0_ATTRIBUTE_CONFLICT_ENABLED", "0")
    monkeypatch.setattr(s.ingest, "search_memories", AsyncMock(return_value=[{**s.mem.get("old"), "score": .1}]))
    facts = [fact(text, attribute="habit.coffee", request_id=event) for event in ("event-1", "event-2")]
    real = s.reinforce.reinforce_existing_sync
    def fail_second(mem, fid, *, event_id, source_timestamp=None):
        return False if event_id == "event-2" else real(mem, fid, event_id=event_id, source_timestamp=source_timestamp)
    monkeypatch.setattr(s.reinforce, "reinforce_existing_sync", fail_second)
    result = await s.ingest.add_memories(user_id="u", pre_extracted_facts=facts)
    assert result["status"] != "ok" and result["retryable"]
    assert s.mem.rows["old"]["metadata"]["mention_count"] == 2
    assert not s.journal.event_recorded("old", "event-2")
    monkeypatch.setattr(s.reinforce, "reinforce_existing_sync", real)
    assert (await s.ingest.add_memories(user_id="u", pre_extracted_facts=facts))["status"] == "ok"
    assert s.mem.rows["old"]["metadata"]["mention_count"] == 3
    assert s.journal.event_recorded("old", "event-2")


@pytest.mark.asyncio
async def test_existing_predecessor_chain_is_completed_on_redelivery(setup, monkeypatch):
    s = setup
    s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "京都に住む", "metadata": {
        "attribute": "residence", "source_timestamp": 100}}
    facts = [fact("東京に住む", source_timestamp=200, source_offset=0),
             fact("大阪に住む", source_timestamp=300, source_offset=20)]
    first = await s.ingest.add_memories(user_id="u", metadata={"batch_id": "chain"}, pre_extracted_facts=facts)
    assert first["status"] == "ok", first
    async def fresh_search(*args, **kwargs):
        return [{**deepcopy(row), "score": .1} for row in s.mem.rows.values() if not row["metadata"].get("valid_to")]
    monkeypatch.setenv("MEM0_CLIENT_DEDUP_ENABLED", "1")
    monkeypatch.setattr(s.ingest, "search_memories", fresh_search)
    for _ in range(2):
        result = await s.ingest.add_memories(user_id="u", metadata={"batch_id": "chain"}, pre_extracted_facts=facts)
        assert result["status"] == "ok"
    assert s.mem.calls == 2
    assert s.mem.rows["old"]["metadata"]["superseded_by"] == "new-1"
    assert s.mem.rows["new-1"]["metadata"]["superseded_by"] == "new-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["owner", "scope", "cycle"])
async def test_lineage_repair_does_not_accept_unrelated_chain(setup, bad):
    s = setup
    for fid, successor in (("old", "middle"), ("middle", "new"), ("new", None)):
        s.mem.rows[fid] = {"id": fid, "user_id": "u", "memory": fid, "metadata": {
            "attribute": "residence", **({"valid_to": 1, "superseded_by": successor} if successor else {})}}
    if bad == "owner":
        s.mem.rows["middle"]["user_id"] = "other"
    elif bad == "scope":
        s.mem.rows["middle"]["metadata"]["scope"] = "conversation:9"
    else:
        s.mem.rows["middle"]["metadata"]["superseded_by"] = "old"
    assert not await s.ingest._archive_chain_reaches(s.mem, "old", "new", "u", {"attribute": "residence"})


@pytest.mark.asyncio
async def test_attribute_lookup_failure_is_not_acknowledged(setup):
    s = setup
    s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "東京に住む", "metadata": {
        "attribute": "residence", "source_timestamp": 100}}
    s.mem.lookup_fails = True
    kwargs = {"user_id": "u", "metadata": {"request_id": "move", "source_timestamp": 200},
              "pre_extracted_facts": [fact("大阪に住む")]}
    result = await s.ingest.add_memories(**kwargs)
    assert result["status"] == "error" and result["retryable"] and s.mem.calls == 0
    assert not s.mem.rows["old"]["metadata"].get("valid_to")
    s.mem.lookup_fails = False
    result = await s.ingest.add_memories(**kwargs)
    assert result["status"] == "ok" and result["archived"] == 1


@pytest.mark.asyncio
async def test_unknown_classifier_does_not_acknowledge_update(setup, monkeypatch):
    s = setup
    s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "東京に住む", "metadata": {"attribute": "residence"}}
    monkeypatch.setattr(s.conflict, "classify_conflict", AsyncMock(return_value={"conflict_type": "addition", "retryable": True}))
    result = await s.ingest.add_memories(user_id="u", pre_extracted_facts=[fact("大阪に住む")])
    assert result["status"] == "error" and s.mem.calls == 0


@pytest.mark.asyncio
async def test_old_confirmation_event_survives_bounded_metadata_window(setup):
    s = setup
    for index in range(130):
        result = await s.ingest.add_memories(user_id="u", metadata={"request_id": f"event-{index}"},
                                           pre_extracted_facts=[fact("東京に住む")])
        assert result["status"] == "ok"
    assert s.mem.rows["new-1"]["metadata"]["mention_count"] == 130
    s.journal._INITIALIZED.clear()  # Simulate a new process using the same private ledger.
    result = await s.ingest.add_memories(user_id="u", metadata={"request_id": "event-1"},
                                       pre_extracted_facts=[fact("東京に住む")])
    assert result["status"] == "ok" and s.mem.calls == 1
    assert s.mem.rows["new-1"]["metadata"]["mention_count"] == 130


def test_confirmation_commit_then_timeout_reconciles_without_double_count(setup):
    s = setup
    s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "coffee", "metadata": {"mention_count": 1}}
    s.mem.update_timeout = True
    assert not s.reinforce.reinforce_existing_sync(s.mem, "old", event_id="e1")
    assert s.mem.rows["old"]["metadata"]["mention_count"] == 2
    assert not s.reinforce.reinforce_existing_sync(s.mem, "old", event_id="e1")
    assert s.journal.event_recorded("old", "e1")
    assert s.mem.rows["old"]["metadata"]["mention_count"] == 2
    assert s.reinforce.reinforce_existing_sync(s.mem, "old", event_id="e2")
    assert s.mem.rows["old"]["metadata"]["mention_count"] == 3


def test_concurrent_confirmation_events_preserve_every_increment(setup):
    s = setup
    s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "coffee", "metadata": {"mention_count": 1}}
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda index: s.reinforce.reinforce_existing_sync(s.mem, "old", event_id=f"e{index}"), range(16)))
    assert all(outcomes) and s.mem.rows["old"]["metadata"]["mention_count"] == 17


@pytest.mark.asyncio
async def test_long_fact_comparison_keeps_both_different_tails(setup, monkeypatch):
    s = setup
    from llm_mem0 import dedup as module
    prefix = "共通の経歴です。" * 50
    captured = {}
    async def complete(**kwargs):
        captured["prompt"] = kwargs["user_message"]
        return {"relation": "different"}
    monkeypatch.setattr(module, "complete_json", complete)
    result = await s.real_judge(prefix + "東京に住む", prefix + "大阪に住む")
    assert result == "different" and "東京に住む" in captured["prompt"] and "大阪に住む" in captured["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("member,operation,condition,archived", [
    ("コーヒー", "retract", "", 1), ("", "retract", "", 0),
    ("紅茶", "retract", "", 0), ("コーヒー", "assert", "", 0),
    ("コーヒー", "retract", "time:night", 0),
])
async def test_explicit_set_member_cancellation_preserves_unrelated_members(setup, member, operation, condition, archived):
    s = setup
    for fid, text, attr in (("coffee", "コーヒーを毎日飲む", "habit.coffee"), ("tea", "紅茶を毎日飲む", "habit.tea")):
        s.mem.rows[fid] = {"id": fid, "user_id": "u", "memory": text, "metadata": {"attribute": attr, "source_timestamp": 100}}
    result = await s.ingest.add_memories(user_id="u", metadata={"request_id": "stop", "source_timestamp": 200},
        pre_extracted_facts=[fact("コーヒーを飲む習慣はやめた", attribute="habit.coffee", member_key=member,
                                  operation=operation, condition=condition)])
    assert result["status"] == "ok" and result["archived"] == archived
    assert not s.mem.rows["tea"]["metadata"].get("valid_to")


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["もしコーヒーをやめたら", "コーヒーをやめたかもしれない", "コーヒーをやめた？", "コーヒーをやめたと言っていた"])
async def test_ambiguous_or_quoted_retraction_never_archives(setup, quote):
    s = setup
    s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "コーヒーを飲む", "metadata": {"attribute": "habit.coffee"}}
    result = await s.ingest.add_memories(user_id="u", pre_extracted_facts=[fact(
        "コーヒーをやめた", attribute="habit.coffee", source_quote=quote, operation="retract", member_key="コーヒー")])
    assert result["status"] == "ok" and result["archived"] == 0


@pytest.mark.asyncio
async def test_reaffirmation_can_end_a_grounded_set_member_retraction(setup):
    s = setup
    s.mem.rows["old"] = {"id": "old", "user_id": "u", "memory": "コーヒーをやめた", "metadata": {
        "attribute": "habit.coffee", "operation": "retract", "member_key": "コーヒー", "source_timestamp": 100}}
    result = await s.ingest.add_memories(user_id="u", metadata={"source_timestamp": 200}, pre_extracted_facts=[fact(
        "コーヒーをまた飲むようになった", attribute="habit.coffee", operation="assert", member_key="コーヒー")])
    assert result["status"] == "ok" and result["archived"] == 1


def backend_for(setup, monkeypatch, text="TAIL"):
    calls = []
    async def complete(**kwargs):
        chunk = re.search(r"<user_text>\n(.*?)\n</user_text>", kwargs["user_message"], re.S).group(1)
        calls.append(chunk)
        return json.dumps({"facts": [fact(text)] if text in chunk else []})
    monkeypatch.setattr(setup.extract, "get_auth_backend", lambda: SimpleNamespace(complete=complete))
    return calls


@pytest.mark.asyncio
async def test_long_input_continues_at_next_window_and_reuses_completed_job(setup, monkeypatch):
    s = setup
    calls = backend_for(s, monkeypatch)
    kwargs = {"user_id": "u", "user_text": "前" * 300 + "TAIL", "metadata": {"request_id": "long"}}
    first = await s.ingest.add_memories(**kwargs)
    assert first["status"] == "partial" and first["next_offset"] > 0 and s.mem.calls == 0
    second = await s.ingest.add_memories(**kwargs)
    assert second["status"] == "ok" and s.mem.calls == 1
    assert any("TAIL" in chunk for chunk in calls)
    count = len(calls)
    third = await s.ingest.add_memories(**kwargs)
    assert third["status"] == "ok" and third["replayed"] and len(calls) == count
    del s.mem.rows["new-1"]
    deleted = await s.ingest.add_memories(**kwargs)
    assert deleted["status"] == "error" and s.mem.calls == 1


@pytest.mark.asyncio
async def test_long_input_without_request_id_returns_explicit_continuation(setup, monkeypatch):
    s = setup
    calls = backend_for(s, monkeypatch)
    result = await s.ingest.add_memories(user_id="u", user_text="前" * 300 + "TAIL")
    assert result["status"] == "error" and result["continuation_id"].startswith("continuation:")
    assert not calls and not s.mem.calls


@pytest.mark.asyncio
async def test_request_id_cannot_mix_changed_input_or_changed_source(setup, monkeypatch):
    s = setup
    backend_for(s, monkeypatch)
    first = await s.ingest.add_memories(user_id="u", user_text="前" * 300, metadata={"request_id": "same"})
    assert first["status"] == "partial"
    changed = await s.ingest.add_memories(user_id="u", user_text="短い別入力", metadata={"request_id": "same"})
    assert changed["status"] == "error" and "request_id_input_mismatch" in changed["error"]


@pytest.mark.asyncio
async def test_extraction_checkpoint_survives_provider_failure_and_budget_change(setup, monkeypatch):
    s = setup
    calls = backend_for(s, monkeypatch, text="HEAD")
    kwargs = {"user_id": "u", "user_text": "HEAD" + "前" * 500, "metadata": {"request_id": "retry"}}
    s.mem.fail_add = 1
    first = await s.ingest.add_memories(**kwargs)
    assert first["status"] == "error"
    call_count = len(calls)
    monkeypatch.setenv("MEM0_EXTRACT_MAX_CHUNKS", "4")
    second = await s.ingest.add_memories(**kwargs)
    assert second["status"] == "partial" and len(calls) == call_count
    assert second["next_offset"] < 240
    final = await s.ingest.add_memories(**kwargs)
    assert final["status"] == "ok"


@pytest.mark.asyncio
async def test_same_progress_job_is_serialized_without_duplicate_provider_writes(setup, monkeypatch):
    s = setup
    backend_for(s, monkeypatch)
    kwargs = {"user_id": "u", "user_text": "前" * 300 + "TAIL", "metadata": {"request_id": "parallel"}}
    results = await asyncio.gather(s.ingest.add_memories(**kwargs), s.ingest.add_memories(**kwargs))
    assert sorted(result["status"] for result in results) == ["ok", "partial"]
    assert s.mem.calls == 1


@pytest.mark.asyncio
async def test_committed_progress_checkpoint_with_lost_response_does_not_repeat_window(setup, monkeypatch):
    s = setup
    calls = backend_for(s, monkeypatch)
    original = s.journal.advance_job
    failed = False
    def commit_then_fail(*args, **kwargs):
        nonlocal failed
        original(*args, **kwargs)
        if not failed:
            failed = True
            raise TimeoutError("synthetic checkpoint response lost")
    monkeypatch.setattr(s.journal, "advance_job", commit_then_fail)
    kwargs = {"user_id": "u", "user_text": "前" * 300 + "TAIL", "metadata": {"request_id": "checkpoint"}}
    first = await s.ingest.add_memories(**kwargs)
    assert first["status"] == "error"
    assert len(calls) == 2
    second = await s.ingest.add_memories(**kwargs)
    assert second["status"] == "ok" and len(calls) == 3 and s.mem.calls == 1


@pytest.mark.asyncio
async def test_each_fact_keeps_original_message_source_and_timestamp(setup, monkeypatch):
    s = setup
    async def complete(**kwargs):
        return json.dumps({"facts": [fact("東京に住む"), fact("大阪に住む")]})
    monkeypatch.setattr(s.extract, "get_auth_backend", lambda: SimpleNamespace(complete=complete))
    result = await s.ingest.add_memories(user_id="u", metadata={"batch_id": "batch", "source_timestamp": 100}, messages=[
        {"role": "user", "content": "東京に住む", "source_id": "discord:1", "author_id": "u", "source_timestamp": 100},
        {"role": "assistant", "content": "response", "source_id": "assistant:1"},
        {"role": "user", "content": "大阪に住む", "source_id": "discord:2", "author_id": "u", "source_timestamp": 200},
    ])
    assert result["status"] == "ok"
    rows = {row["memory"]: row["metadata"] for row in s.mem.rows.values()}
    assert rows["東京に住む"]["source_timestamp"] == 100
    assert rows["大阪に住む"]["source_timestamp"] == 200
    assert json.loads(rows["大阪に住む"]["source_message_ids"]) == ["discord:2"]
    assert rows["大阪に住む"]["request_id"] == "discord:2"


def test_llm_fields_cannot_create_internal_archive_plans(setup):
    normalized = setup.extract.normalize_fact(fact("大阪へ旅行した", _predecessors=[["old", "update"]],
        _historical_successor=["old", "correction"], ingest_key="forged", pending_predecessors="forged"))
    assert normalized is not None
    assert not any(key.startswith("_") or key in {"ingest_key", "pending_predecessors"} for key in normalized)
    assert setup.extract.normalize_fact(fact("東京に住む", scope="conversation:999")) is None
    assert setup.extract.normalize_fact(fact("東京に住む", scope="conversation:999"), programmatic=True) is not None


def test_quality_journal_rejects_symlink_without_touching_target(setup, tmp_path):
    target = tmp_path / "untouched"
    target.write_text("unchanged")
    (tmp_path / "mem0_quality_journal.sqlite3").symlink_to(target)
    with pytest.raises(OSError):
        setup.journal.event_recorded("fact", "event")
    assert target.read_text() == "unchanged"

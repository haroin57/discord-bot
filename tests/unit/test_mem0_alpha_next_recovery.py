"""Idle durable recovery acceptance; all input and providers are synthetic."""
import asyncio
import json
import os
import threading
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(scope="module", autouse=True)
def alpha_bootstrap():
    import services.mem0 as package
    return package


@pytest.fixture
def batch():
    from services.mem0 import batch
    return batch


@pytest.fixture
def clock(batch, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    return now


def scan(batch, path, **kwargs):
    return batch.scan_ready_batches(path, allowed_user_ids={"a", "b"}, **kwargs)


def test_age_uses_enqueue_not_historical_source_time(tmp_path, batch, clock):
    batch.append_turn(tmp_path, 1, "backfill", "", user_id="a",
                      source_timestamp="2000-01-01T00:00:00Z")
    assert scan(batch, tmp_path, max_age_seconds=10)["ready"] == []
    clock[0] += 10
    assert scan(batch, tmp_path, max_age_seconds=10)["ready"] == [(1, "a")]


def test_legacy_timestamp_age_retains_unowned_input(tmp_path, batch, clock):
    batch.append_turn(tmp_path, 1, "legacy", "", source_timestamp="1970-01-01T00:00:01Z")
    batch.append_turn(tmp_path, 1, "old attributed", "", user_id="a",
                      source_timestamp="1970-01-01T00:00:01Z")
    data = batch._load_raw(tmp_path, 1)
    for turn in data["turns"]:
        turn.pop("enqueued_at")
    batch._save_raw(tmp_path, 1, data)
    result = scan(batch, tmp_path, max_age_seconds=10)
    assert result["ready"] == [(1, "a")]
    assert result["unowned_turns"] == 1 and result["pending_turns"] == 2
    assert "legacy" not in json.dumps(result)


def test_active_lease_excluded_then_expired_same_id(tmp_path, batch, clock):
    batch.append_turn(tmp_path, 1, "input", "", user_id="a", force_flush=True)
    old = batch.claim_batch(tmp_path, 1, user_id="a", lease_seconds=10)
    assert scan(batch, tmp_path)["ready"] == []
    clock[0] += 10
    assert scan(batch, tmp_path)["ready"] == [(1, "a")]
    new = batch.claim_batch(tmp_path, 1, user_id="a")
    assert new.batch_id == old.batch_id and new.token != old.token
    assert not batch.ack_batch(tmp_path, old)


def test_backoff_persists_exponentially_and_caps(tmp_path, batch, clock):
    batch.append_turn(tmp_path, 1, "input", "", user_id="a", force_flush=True)
    identity = None
    for delay in (10, 20, 25, 25):
        lease = batch.claim_batch(tmp_path, 1, user_id="a")
        identity = identity or lease.batch_id
        assert lease.batch_id == identity
        assert batch.retry_batch(tmp_path, lease, delay_seconds=10, max_delay_seconds=25)
        data = batch._load_raw(tmp_path, 1)
        assert data["inflight"][identity]["next_retry_at"] == clock[0] + delay
        assert scan(batch, tmp_path)["backoff_batches"] == 1
        assert scan(batch, tmp_path)["ready"] == []
        assert batch.claim_batch(tmp_path, 1, user_id="a") is None
        clock[0] += delay
        assert scan(batch, tmp_path)["ready"] == [(1, "a")]


@pytest.mark.parametrize("options", [{"max_files": 0}, {"limit": 0}, {"max_files": True},
                                        {"max_age_seconds": float("nan")}, {"max_age_seconds": -1}])
def test_invalid_scan_budget_rejected(tmp_path, batch, options):
    with pytest.raises(ValueError):
        scan(batch, tmp_path, **options)


def test_scan_does_not_create_state_without_queue(tmp_path, batch):
    absent = tmp_path / "absent"
    assert scan(batch, absent)["ready"] == []
    assert not absent.exists()


def test_cursor_skips_nonready_files_with_single_file_budget(tmp_path, batch, clock):
    batch.append_turn(tmp_path, 1, "waiting", "", user_id="a")
    batch.append_turn(tmp_path, 2, "ready", "", user_id="b", force_flush=True)
    first = scan(batch, tmp_path, max_files=1)
    assert first["ready"] == [] and first["scanned_files"] == 1
    second = scan(batch, tmp_path, max_files=1, cursor=first["cursor"])
    assert second["ready"] == [(2, "b")] and second["scanned_files"] == 1


def test_scan_cursor_fair_across_owners(tmp_path, batch):
    for channel in (1, 2):
        for owner in ("a", "b"):
            batch.append_turn(tmp_path, channel, "ready", "", user_id=owner, force_flush=True)
    cursor = None
    seen = []
    for _ in range(4):
        result = scan(batch, tmp_path, limit=1, cursor=cursor)
        seen.extend(result["ready"])
        cursor = result["cursor"]
    assert seen == [(1, "a"), (1, "b"), (2, "a"), (2, "b")]


def test_single_file_budget_advances_past_still_ready_owner(tmp_path, batch):
    for channel in (1, 2):
        batch.append_turn(tmp_path, channel, "ready", "", user_id="a", force_flush=True)
    cursor, seen = None, []
    for _ in range(3):
        result = scan(batch, tmp_path, max_files=1, limit=1, cursor=cursor)
        seen.extend(result["ready"])
        cursor = result["cursor"]
    assert seen == [(1, "a"), (2, "a")]


def test_scan_preserves_corrupt_and_symlink_files(tmp_path, batch):
    corrupt = batch._batch_file(tmp_path, 1)
    corrupt.write_text('{"turns":', encoding="utf-8")
    target = tmp_path / "protected.json"
    target.write_text("protected", encoding="utf-8")
    batch._batch_file(tmp_path, 2).symlink_to(target)
    batch.append_turn(tmp_path, 3, "ready", "", user_id="a", force_flush=True)
    result = scan(batch, tmp_path)
    assert result["ready"] == [(3, "a")] and result["invalid_files"] == 2
    assert corrupt.read_text() == '{"turns":' and target.read_text() == "protected"


@pytest.mark.asyncio
async def test_recovery_without_new_message_keeps_identity_and_owner(tmp_path, batch, clock):
    import bot_record
    batch.append_turn(tmp_path, 1, "input", "reply", user_id="a", author_id="a",
                      request_id="request-1", message_id="111", force_flush=True)
    old = batch.claim_batch(tmp_path, 1, user_id="a", lease_seconds=1)
    clock[0] += 2
    provider = AsyncMock(return_value={"status": "ok"})
    result = await bot_record.recover_alpha_batches_once(
        add_memories=provider, allowed_user_ids={"a"}, state_dir=tmp_path)
    assert result["completed"] == 1 and result["selected"] == 1
    kwargs = provider.await_args.kwargs
    assert kwargs["user_id"] == "a" and kwargs["metadata"]["batch_id"] == old.batch_id
    assert kwargs["messages"][0]["source_id"] == "discord:111"
    assert batch.batch_status(tmp_path, 1)["inflight_batches"] == 0


@pytest.mark.asyncio
async def test_recovery_age_flush_and_per_turn_provenance(tmp_path, batch, clock):
    import bot_record
    for n in (1, 2):
        batch.append_turn(tmp_path, 1, f"fact-{n}", "reply", user_id="a", author_id="a",
                          request_id=f"r{n}", message_id=str(n), channel_name="fixture",
                          source_timestamp=f"2026-01-0{n}T00:00:00Z")
    clock[0] += 10
    provider = AsyncMock(return_value={"status": "ok"})
    result = await bot_record.recover_alpha_batches_once(
        add_memories=provider, allowed_user_ids={"a"}, state_dir=tmp_path, max_age_seconds=10)
    assert result["completed"] == 1
    messages = provider.await_args.kwargs["messages"]
    assert [m["source_id"] for m in messages[::2]] == ["discord:1", "discord:2"]
    assert [m["source_timestamp"] for m in messages[::2]] == ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]
    assert all("author_id" not in m and "source_timestamp" not in m for m in messages[1::2])
    assert provider.await_args.kwargs["metadata"]["channel_name"] == "fixture"


@pytest.mark.asyncio
async def test_recovery_bounded_and_honors_permission_revocation(tmp_path, batch):
    import bot_record
    allowed = {"a", "b"}
    for owner in ("", "a", "b", "other"):
        batch.append_turn(tmp_path, 1, "input", "", user_id=owner, force_flush=True)
    async def provider(**kwargs):
        assert kwargs["user_id"] == "a"
        allowed.remove("b")
        return {"status": "ok"}
    result = await bot_record.recover_alpha_batches_once(
        add_memories=provider, allowed_user_ids=allowed, state_dir=tmp_path, max_batches=2)
    assert result["completed"] == 1
    assert batch.batch_status(tmp_path, 1)["pending_turns"] == 3


@pytest.mark.asyncio
async def test_recovery_failure_backoff_then_success(tmp_path, batch, clock):
    import bot_record
    batch.append_turn(tmp_path, 1, "input", "", user_id="a", force_flush=True)
    provider = AsyncMock(side_effect=[{"status": "partial"}, {"status": "ok"}])
    kwargs = dict(add_memories=provider, allowed_user_ids={"a"}, state_dir=tmp_path,
                  retry_base_seconds=10, retry_max_seconds=20)
    assert (await bot_record.recover_alpha_batches_once(**kwargs))["completed"] == 0
    assert (await bot_record.recover_alpha_batches_once(**kwargs))["selected"] == 0
    assert provider.await_count == 1
    clock[0] += 10
    assert (await bot_record.recover_alpha_batches_once(**kwargs))["completed"] == 1
    assert provider.await_args_list[0].kwargs["metadata"]["batch_id"] == provider.await_args_list[1].kwargs["metadata"]["batch_id"]


@pytest.mark.asyncio
async def test_continuation_progress_runs_next_bounded_tick_without_failure_backoff(tmp_path, batch):
    import bot_record
    batch.append_turn(tmp_path, 1, "input", "", user_id="a", force_flush=True)
    async def provider(**kwargs):
        return {"status": "partial", "error": "continuation_required", "next_offset": 2,
                "total_chars": 5, "continuation_id": kwargs["metadata"]["batch_id"]}
    kwargs = dict(add_memories=provider, allowed_user_ids={"a"}, state_dir=tmp_path)
    result = await bot_record.recover_alpha_batches_once(**kwargs)
    assert result["completed"] == 0 and result["selected"] == 1
    data = batch._load_raw(tmp_path, 1)
    pending = next(iter(data["inflight"].values()))
    assert pending["retry_count"] == 0
    assert batch.has_retry_batch(tmp_path, 1, user_id="a")
    assert (await bot_record.recover_alpha_batches_once(**kwargs))["selected"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"status": "ok", "results": "bad"}, {"status": "ok", "failed": True},
                                      {"status": "unknown", "results": []}, {"results": ["bad"]}])
async def test_malformed_success_is_not_acknowledged(tmp_path, batch, result):
    import bot_record
    batch.append_turn(tmp_path, 1, "input", "", user_id="a", force_flush=True)
    outcome = await bot_record.recover_alpha_batches_once(
        add_memories=AsyncMock(return_value=result), allowed_user_ids={"a"}, state_dir=tmp_path)
    assert outcome["completed"] == 0
    assert batch.batch_status(tmp_path, 1)["inflight_batches"] == 1


@pytest.mark.asyncio
async def test_recovery_concurrent_workers_deliver_once(tmp_path, batch):
    import bot_record
    batch.append_turn(tmp_path, 1, "input", "", user_id="a", force_flush=True)
    provider = AsyncMock(return_value={"status": "ok"})
    kwargs = dict(add_memories=provider, allowed_user_ids={"a"}, state_dir=tmp_path)
    result = await asyncio.gather(*(bot_record.recover_alpha_batches_once(**kwargs) for _ in range(4)))
    assert sum(row["completed"] for row in result) == 1
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_io_cancellation_waits_for_actual_thread():
    import bot_record
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def operation():
        started.set()
        assert release.wait(5)
        finished.set()
    task = asyncio.create_task(bot_record._batch_io(operation))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_worker_disabled_does_not_call_provider(monkeypatch):
    import bot_record
    from llm_mem0.settings import settings
    monkeypatch.setattr(settings, "MEM0_BATCH_RECOVERY_ENABLED", False)
    provider = AsyncMock()
    await bot_record.alpha_recovery_worker(add_memories=provider, allowed_user_ids={"a"})
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_starts_immediately_and_obeys_runtime_cancellation(monkeypatch):
    import bot_record
    from llm_mem0.settings import settings
    monkeypatch.setattr(settings, "MEM0_BATCH_RECOVERY_ENABLED", True)
    monkeypatch.setattr(settings, "MEM0_BATCH_RECOVERY_INTERVAL_S", 30.0)
    allowed, started = {"a"}, asyncio.Event()
    recover = AsyncMock(return_value={"cursor": (1, "a"), "completed": 1, "selected": 1})
    async def repair():
        started.set()
        return {"completed": 0, "failed": 0, "pending": 0}
    monkeypatch.setattr(bot_record, "recover_alpha_batches_once", recover)
    monkeypatch.setattr(bot_record, "_recover_alpha_lineage_once", repair)
    task = asyncio.create_task(bot_record.alpha_recovery_worker(
        add_memories=AsyncMock(), allowed_user_ids=allowed))
    try:
        await asyncio.wait_for(started.wait(), 3)
        recover.assert_awaited_once()
        assert recover.await_args.kwargs["allowed_user_ids"] is allowed
        assert recover.await_args.kwargs["max_batches"] == settings.MEM0_BATCH_RECOVERY_MAX_BATCHES
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_worker_rejects_invalid_interval_before_processing(monkeypatch):
    import bot_record
    from llm_mem0.settings import settings
    monkeypatch.setattr(settings, "MEM0_BATCH_RECOVERY_ENABLED", True)
    monkeypatch.setattr(settings, "MEM0_BATCH_RECOVERY_INTERVAL_S", float("nan"))
    recover = AsyncMock()
    monkeypatch.setattr(bot_record, "recover_alpha_batches_once", recover)
    with pytest.raises(ValueError, match="timing"):
        await bot_record.alpha_recovery_worker(add_memories=AsyncMock(), allowed_user_ids={"a"})
    recover.assert_not_awaited()


@pytest.mark.asyncio
async def test_lineage_idle_does_not_initialize_provider(monkeypatch):
    import bot_record
    from llm_mem0 import client, lineage
    monkeypatch.setattr(lineage, "has_pending_repairs", lambda: False)
    def no_provider():
        pytest.fail("idle repair initialized provider")
    monkeypatch.setattr(client, "_get_mem0", no_provider)
    assert await bot_record._recover_alpha_lineage_once() == {"completed": 0, "failed": 0, "pending": 0}


@pytest.mark.asyncio
async def test_lineage_worker_uses_bounded_pending_repair(monkeypatch):
    import bot_record
    from llm_mem0 import client, lineage
    provider = object()
    monkeypatch.setattr(lineage, "has_pending_repairs", lambda: True)
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    def repair(mem, *, limit):
        assert mem is provider and limit == 100
        return {"completed": 1, "failed": 0, "pending": 0}
    monkeypatch.setattr(lineage, "repair_dependents_sync", repair)
    assert (await bot_record._recover_alpha_lineage_once())["completed"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, None, {"results": "invalid"}, {"results": ["invalid"]}])
async def test_strict_enumeration_rejects_malformed_response(monkeypatch, response):
    from llm_mem0 import client
    monkeypatch.setattr(client, "_get_mem0", object)
    monkeypatch.setattr(client, "mem_get_all", lambda *a, **k: response)
    with pytest.raises(RuntimeError, match="malformed"):
        await client.get_all_memories("a", strict=True)
    assert await client.get_all_memories("a") == []


@pytest.mark.asyncio
async def test_failed_delete_intent_prevents_primary_mutation(monkeypatch):
    from llm_mem0 import client, lineage
    class Memory:
        def __init__(self):
            self.deleted = False
        def get(self, _):
            return {"id": "source", "memory": "fact", "user_id": "a", "metadata": {}}
        def delete(self, _):
            self.deleted = True
    memory = Memory()
    monkeypatch.setattr(client, "_get_mem0", lambda: memory)
    def fail(*args, **kwargs):
        raise OSError("synthetic intent failure")
    monkeypatch.setattr(lineage, "prepare_source_delete", fail)
    assert not await client.delete_memory("source")
    assert not memory.deleted

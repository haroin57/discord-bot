"""Durable alpha recording, using the actual bot-to-alpha batch bridge."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
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


def test_request_id_redelivery_survives_ack(tmp_path, batch):
    assert batch.append_turn(tmp_path, 1, "first", "reply", request_id="r1", user_id="a") == 1
    assert batch.append_turn(tmp_path, 1, "first", "reply", request_id="r1", user_id="a") == 1
    lease = batch.claim_batch(tmp_path, 1, user_id="a")
    assert lease.turns[0]["user"] == "first"
    assert batch.claim_batch(tmp_path, 1, user_id="a") is None
    assert batch.ack_batch(tmp_path, lease)
    assert batch.append_turn(tmp_path, 1, "first", "reply", request_id="r1", user_id="a") == 0


def test_failure_retains_stable_batch_and_fences_old_token(tmp_path, batch):
    batch.append_turn(tmp_path, 1, "retained", "", user_id="a")
    first = batch.claim_batch(tmp_path, 1, user_id="a")
    assert batch.retry_batch(tmp_path, first, "fixture outage")
    assert batch.has_retry_batch(tmp_path, 1, user_id="a")
    retry = batch.claim_batch(tmp_path, 1, user_id="a")
    assert retry.batch_id == first.batch_id
    assert retry.token != first.token
    assert retry.turns == first.turns
    assert not batch.ack_batch(tmp_path, first)
    assert not batch.retry_batch(tmp_path, first)
    assert batch.ack_batch(tmp_path, retry)


def test_expired_claim_is_recoverable_after_restart(tmp_path, batch, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(batch.time, "time", lambda: clock[0])
    batch.append_turn(tmp_path, 1, "persistent", "", user_id="a")
    old = batch.claim_batch(tmp_path, 1, user_id="a", lease_seconds=5)
    assert batch.renew_batch(tmp_path, old, lease_seconds=10)
    clock[0] = 1011
    assert not batch.ack_batch(tmp_path, old)
    new = batch.claim_batch(tmp_path, 1, user_id="a")
    assert new.batch_id == old.batch_id and new.turns[0]["user"] == "persistent"


def test_speakers_and_unknown_legacy_turns_remain_separate(tmp_path, batch):
    batch.append_turn(tmp_path, 1, "unowned old input", "")
    batch.append_turn(tmp_path, 1, "A fact", "", user_id="a")
    batch.append_turn(tmp_path, 1, "B fact", "", user_id="b")
    a = batch.claim_batch(tmp_path, 1, user_id="a")
    b = batch.claim_batch(tmp_path, 1, user_id="b")
    assert [t["user"] for t in a.turns] == ["A fact"]
    assert [t["user"] for t in b.turns] == ["B fact"]
    assert batch.batch_status(tmp_path, 1)["unowned_turns"] == 1
    assert batch.ack_batch(tmp_path, a) and batch.ack_batch(tmp_path, b)
    old = batch.pop_batch(tmp_path, 1)
    turns, facts = old
    assert turns[0]["user"] == "unowned old input" and facts == []
    assert batch.ack_batch(tmp_path, old)


def test_append_during_claim_is_not_acknowledged_with_old_batch(tmp_path, batch):
    batch.append_turn(tmp_path, 1, "old", "", user_id="a")
    old = batch.claim_batch(tmp_path, 1, user_id="a")
    batch.append_turn(tmp_path, 1, "new", "", user_id="a")
    assert batch.ack_batch(tmp_path, old)
    assert batch.claim_batch(tmp_path, 1, user_id="a").turns[0]["user"] == "new"


def test_full_text_survives_and_oversize_is_explicit(tmp_path, batch):
    text = "長文" * 3000 + "末尾の重要な事実"
    batch.append_turn(tmp_path, 1, text, "", user_id="a")
    lease = batch.claim_batch(tmp_path, 1, user_id="a")
    assert lease.turns[0]["user"] == text
    assert "末尾の重要な事実" in batch.build_batch_summary(lease.turns, batch.MAX_TURN_CHARS)
    with pytest.raises(ValueError, match="exceeds"):
        batch.append_turn(tmp_path, 1, "x" * (batch.MAX_TURN_CHARS + 1), "")


def test_corrupt_file_is_not_overwritten(tmp_path, batch):
    path = batch._batch_file(tmp_path, 1)
    path.write_text('{"turns":', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(json.JSONDecodeError):
        batch.append_turn(tmp_path, 1, "new", "")
    assert path.read_bytes() == before


def test_threads_do_not_lose_appends(tmp_path, batch):
    def append(i):
        batch.append_turn(tmp_path, 1, str(i), "", user_id="a", request_id=str(i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(80)))
    assert batch.batch_status(tmp_path, 1)["pending_turns"] == 80


def test_processes_do_not_lose_appends(tmp_path, batch):
    source = Path(batch.__file__).resolve().parent.parent
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from llm_mem0.batch import append_turn; "
        "[append_turn(sys.argv[2], 1, str(i), '', user_id='a', request_id=sys.argv[3]+str(i)) "
        "for i in range(12)]"
    )
    children = [subprocess.Popen([sys.executable, "-c", code, str(source), str(tmp_path), str(i)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
    for child in children:
        _, error = child.communicate(timeout=30)
        assert child.returncode == 0, error.decode()
    assert batch.batch_status(tmp_path, 1)["pending_turns"] == 48


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_leases_rejected(tmp_path, batch, value):
    with pytest.raises(ValueError):
        batch.claim_batch(tmp_path, 1, lease_seconds=value)


def _message(author="a", mid=123):
    return NS(channel=NS(id=1, name="fixture"), author=NS(id=author), id=mid,
              created_at=datetime(2026, 9, 9, tzinfo=timezone.utc))


async def _record(module, provider, tasks, *, author="a", mid=123, force=True, text="覚えて: 住所は東京"):
    def track(coro, **kwargs):
        tasks.append(asyncio.create_task(coro))
    await module.record_turn(
        message=_message(author, mid), raw_user_text=text, response_text="応答です。",
        mem0_user_id=author, conv_context="別の人の会話です。", add_memories=provider,
        track_task=track, load_history_channel=lambda *_: [],
        is_force_record_request=lambda _: force, request_id=f"request-{mid}",
    )


@pytest.mark.asyncio
async def test_real_alpha_bot_bridge_accepts_request_and_preserves_source(tmp_path, batch, monkeypatch):
    import bot_record
    monkeypatch.setattr(bot_record, "STATE_DIR", str(tmp_path))
    provider = AsyncMock(return_value={"status": "ok", "results": [], "kept": 0})
    tasks = []
    await _record(bot_record, provider, tasks)
    await asyncio.gather(*tasks)
    kwargs = provider.await_args.kwargs
    assert kwargs["user_id"] == "a"
    assert kwargs["messages"][0]["content"] == "覚えて: 住所は東京"
    assert kwargs["messages"][1]["role"] == "assistant"
    assert json.loads(kwargs["metadata"]["source_message_ids"]) == ["123"]
    assert kwargs["metadata"]["source_author_id"] == "a"
    assert batch.batch_status(tmp_path, 1)["inflight_batches"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {"error": "outage"}, {"status": "partial"}, {"failed": 1}])
async def test_bot_failure_is_durable_and_retries_next_turn(tmp_path, batch, monkeypatch, result):
    import bot_record
    monkeypatch.setattr(bot_record, "STATE_DIR", str(tmp_path))
    provider = AsyncMock(side_effect=[result, {"status": "ok", "kept": 1}])
    tasks = []
    await _record(bot_record, provider, tasks)
    await asyncio.gather(*tasks)
    assert batch.batch_status(tmp_path, 1)["retry_batches"] == 1
    await _record(bot_record, provider, tasks, mid=124, force=False, text="next turn")
    await asyncio.gather(*tasks)
    assert provider.await_count == 2
    assert provider.await_args.kwargs["metadata"]["batch_id"] == provider.await_args_list[0].kwargs["metadata"]["batch_id"]
    assert batch.batch_status(tmp_path, 1)["pending_turns"] == 1
    assert batch.batch_status(tmp_path, 1)["inflight_batches"] == 0


@pytest.mark.asyncio
async def test_mixed_speaker_bot_batch_never_uses_final_speaker_as_owner(tmp_path, batch, monkeypatch):
    import bot_record
    monkeypatch.setattr(bot_record, "STATE_DIR", str(tmp_path))
    provider = AsyncMock(return_value={"status": "ok"})
    tasks = []
    await _record(bot_record, provider, tasks, author="a", mid=1, force=False, text="A fact")
    await _record(bot_record, provider, tasks, author="b", mid=2, text="B fact")
    await asyncio.gather(*tasks)
    kwargs = provider.await_args.kwargs
    assert kwargs["user_id"] == "b" and "A fact" not in str(kwargs["messages"])
    assert batch.claim_batch(tmp_path, 1, user_id="a").turns[0]["user"] == "A fact"


@pytest.mark.asyncio
async def test_bot_cancellation_keeps_input(tmp_path, batch, monkeypatch):
    import bot_record
    monkeypatch.setattr(bot_record, "STATE_DIR", str(tmp_path))
    started = asyncio.Event()
    async def provider(**kwargs):
        started.set()
        await asyncio.Future()
    tasks = []
    await _record(bot_record, provider, tasks)
    await started.wait()
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    assert batch.batch_status(tmp_path, 1)["retry_batches"] == 1


@pytest.mark.asyncio
async def test_force_record_during_inflight_is_drained_after_ack(tmp_path, batch, monkeypatch):
    """A second explicit remember request must not wait for a third Discord turn."""
    import bot_record
    monkeypatch.setattr(bot_record, "STATE_DIR", str(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return {"status": "ok", "results": [], "kept": 0}

    tasks = []
    try:
        await _record(bot_record, provider, tasks, mid=901, text="first explicit memory")
        await asyncio.wait_for(entered.wait(), timeout=3)
        await _record(bot_record, provider, tasks, mid=902, text="second explicit memory")
        # This contender cannot obtain the still-active first lease.
        await asyncio.wait_for(asyncio.shield(tasks[1]), timeout=3)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
        assert len(calls) == 2
        assert calls[1]["messages"][0]["content"] == "second explicit memory"
        assert batch.batch_status(tmp_path, 1)["pending_turns"] == 0
        assert batch.batch_status(tmp_path, 1)["inflight_batches"] == 0
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_duplicate_append_retries_failed_directory_fsync(tmp_path, batch, monkeypatch):
    """A visible rename is not proof of a previously failed durable append."""
    import stat
    from llm_mem0 import atomic_write
    real_fsync = os.fsync
    attempts = []
    injected = False
    target = batch._batch_file(tmp_path, 1)

    def fail_directory_once(fd):
        nonlocal injected
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        attempts.append(is_directory)
        if is_directory and target.exists() and not injected:
            injected = True
            raise OSError("fixture directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(atomic_write.os, "fsync", fail_directory_once)
    with pytest.raises(OSError, match="directory fsync"):
        batch.append_turn(tmp_path, 1, "retained text", "", user_id="a", request_id="durable-request")
    assert target.is_file(), "The injected failure must occur after replace, not before initial creation"
    attempted_before_retry = len(attempts)
    assert batch.append_turn(tmp_path, 1, "retained text", "", user_id="a", request_id="durable-request") == 1
    assert True in attempts[attempted_before_retry:], "Duplicate retry acknowledged without retrying directory fsync"
    assert batch.batch_status(tmp_path, 1)["pending_turns"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{}, {"status": "pending"}, {"status": "unknown"}])
async def test_bot_requires_positive_ingest_completion_before_ack(tmp_path, batch, monkeypatch, result):
    import bot_record
    monkeypatch.setattr(bot_record, "STATE_DIR", str(tmp_path))
    provider = AsyncMock(return_value=result)
    tasks = []
    await _record(bot_record, provider, tasks, mid=903)
    await asyncio.gather(*tasks)
    status = batch.batch_status(tmp_path, 1)
    assert status["retry_batches"] == 1
    assert status["inflight_batches"] == 1


def test_atomic_write_does_not_close_reused_fd_after_file_fsync_failure(tmp_path, batch, monkeypatch):
    """fdopen owns the descriptor even when its context exits exceptionally."""
    from llm_mem0 import atomic_write
    real_fdopen = os.fdopen
    victim = []

    class ReuseClosedDescriptor:
        def __init__(self, fd, *args, **kwargs):
            self.original_fd = fd
            self.stream = real_fdopen(fd, *args, **kwargs)

        def __enter__(self):
            return self.stream.__enter__()

        def __exit__(self, *args):
            outcome = self.stream.__exit__(*args)
            # Deterministically model another thread opening a file as soon as
            # fdopen closes its owned descriptor during exception unwinding.
            replacement = os.open(tmp_path / "unrelated.txt", os.O_CREAT | os.O_RDWR, 0o600)
            victim.append(replacement)
            assert replacement == self.original_fd
            return outcome

    def fail_fsync(_fd):
        raise OSError("fixture file fsync failure")

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(atomic_write.os, "fdopen", ReuseClosedDescriptor)
            patcher.setattr(atomic_write.os, "fsync", fail_fsync)
            with pytest.raises(OSError, match="file fsync"):
                atomic_write.atomic_write_bytes(tmp_path / "target.txt", b"synthetic")
        assert victim
        # EBADF here proves exception cleanup closed a different operation's FD.
        os.fstat(victim[0])
    finally:
        for fd in victim:
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.asyncio
async def test_reconstructed_discord_message_preserves_original_timestamp(tmp_path, batch, monkeypatch):
    import bot_record
    import discord
    monkeypatch.setattr(bot_record, "STATE_DIR", str(tmp_path))
    original_time = datetime(2026, 9, 8, 12, 34, 56, tzinfo=timezone.utc)
    message_id = discord.utils.time_snowflake(original_time)
    # This is the real outbox replay shape: no created_at attribute survives.
    replay_message = NS(channel=NS(id=1, name="fixture"), author=NS(id="a"), id=message_id)
    provider = AsyncMock(return_value={"status": "ok", "results": [], "kept": 0})
    tasks = []

    def track(coro, **kwargs):
        tasks.append(asyncio.create_task(coro))

    await bot_record.record_turn(
        message=replay_message, raw_user_text="覚えて: yesterday's synthetic fact",
        response_text="fixture", mem0_user_id="a", conv_context="", add_memories=provider,
        track_task=track, load_history_channel=lambda *_: [],
        is_force_record_request=lambda _: True, request_id="stable-outbox-request",
    )
    await asyncio.gather(*tasks)
    metadata = provider.await_args.kwargs["metadata"]
    assert datetime.fromisoformat(metadata["source_timestamp"]) == original_time
    assert json.loads(metadata["source_message_ids"]) == [str(message_id)]

import asyncio
import threading
from unittest.mock import Mock

import pytest

from bot_pipeline.turn_outbox import TurnOutbox


def staged_outbox(tmp_path):
    outbox = TurnOutbox(str(tmp_path / "outbox.json"))
    outbox.begin("rid", request_message_id=1, channel_id=2, user_id=3)
    outbox.prepare("rid", {"response_text": "answer"})
    outbox.record_delivery("rid", message_ids=[4], streamed=False)
    return outbox


@pytest.mark.asyncio
async def test_reconcile_never_writes_on_the_event_loop(tmp_path, monkeypatch):
    outbox = staged_outbox(tmp_path)
    loop_thread = threading.get_ident()
    original = outbox._save
    saves = []
    def save():
        saves.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        original()
    monkeypatch.setattr(outbox, "_save", save)
    called = []
    async def callback(turn):
        assert threading.get_ident() == loop_thread
        called.append(turn["idempotency_step"])
    assert await outbox.reconcile("rid", archive=callback, session=callback, record=callback)
    assert called == ["session", "record"]
    assert len(saves) == 5
    # The bot reconciles once on delivery and again in finally. Repeating an
    # already committed turn must not re-serialize/fsync the retained journal.
    assert await outbox.reconcile("rid", archive=callback, session=callback, record=callback)
    assert len(saves) == 5
    assert called == ["session", "record"]


@pytest.mark.asyncio
async def test_cancelled_receipt_finishes_durably_before_cleanup(tmp_path, monkeypatch):
    outbox = TurnOutbox(str(tmp_path / "outbox.json"))
    outbox.begin("rid", request_message_id=1, channel_id=2, user_id=3)
    entered = threading.Event()
    release = threading.Event()
    original = outbox._save
    def slow_save():
        entered.set()
        assert release.wait(3)
        original()
    monkeypatch.setattr(outbox, "_save", slow_save)
    receipt = asyncio.create_task(outbox.run_io(
        outbox.record_delivery, "rid", message_ids=[4], streamed=True
    ))
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        receipt.cancel("user abort")
        await asyncio.sleep(0)
        receipt.cancel("shutdown")
        await asyncio.sleep(0)
        assert not receipt.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError, match="user abort"):
        await receipt
    reloaded = TurnOutbox(outbox.path).snapshot("rid")
    assert reloaded["delivery"]["message_ids"] == [4]
    assert reloaded["delivery"]["acknowledged"] is True


def test_duplicate_receipts_and_begin_do_not_rewrite_journal(tmp_path, monkeypatch):
    outbox = staged_outbox(tmp_path)
    save = Mock(wraps=outbox._save)
    monkeypatch.setattr(outbox, "_save", save)
    outbox.begin("rid", request_message_id=1, channel_id=2, user_id=3)
    outbox.record_delivery("rid", message_ids=[4], streamed=False)
    save.assert_not_called()
    outbox.record_delivery("rid", message_ids=[4], streamed=True)
    assert save.call_count == 1
    outbox.record_delivery("rid", message_ids=[4, 5], streamed=True)
    assert save.call_count == 2
    assert TurnOutbox(outbox.path).snapshot("rid")["delivery"]["message_ids"] == [4, 5]


@pytest.mark.asyncio
async def test_run_io_reports_write_failure(tmp_path):
    outbox = staged_outbox(tmp_path)
    def failed():
        raise OSError("disk error")
    with pytest.raises(OSError, match="disk error"):
        await outbox.run_io(failed)


def test_duplicate_receipt_retries_a_failed_durable_write(tmp_path, monkeypatch):
    import bot_pipeline.turn_outbox as module
    outbox = staged_outbox(tmp_path)
    original = module.atomic_write_json
    def failed(*args):
        raise OSError("disk full")
    monkeypatch.setattr(module, "atomic_write_json", failed)
    with pytest.raises(OSError, match="disk full"):
        outbox.record_delivery("rid", message_ids=[4, 5], streamed=True)
    assert TurnOutbox(outbox.path).snapshot("rid")["delivery"]["message_ids"] == [4]
    monkeypatch.setattr(module, "atomic_write_json", original)
    outbox.record_delivery("rid", message_ids=[4, 5], streamed=True)
    assert TurnOutbox(outbox.path).snapshot("rid")["delivery"]["message_ids"] == [4, 5]


@pytest.mark.asyncio
async def test_late_archive_reopens_completed_turn_and_survives_restart(tmp_path):
    outbox = staged_outbox(tmp_path)
    async def noop(_):
        pass
    assert await outbox.reconcile("rid", archive=noop, session=noop, record=noop)
    outbox.add_archive_job("rid", paths=["missing-synthetic.png"], kwargs={})
    reopened = TurnOutbox(outbox.path)
    assert len(reopened.pending()) == 1
    archived = []
    async def archive(job):
        archived.append(job["paths"])
    assert await reopened.reconcile("rid", archive=archive, session=noop, record=noop)
    assert archived == [["missing-synthetic.png"]]
    assert reopened.pending() == []


def test_pending_archive_is_not_terminal_even_in_legacy_completed_steps(tmp_path):
    outbox = staged_outbox(tmp_path)
    turn = outbox.snapshot("rid")
    turn["steps"] = {"archive": "done", "session": "done", "record": "done"}
    turn["archive_jobs"] = [{"status": "pending"}]
    assert not outbox._is_terminal(turn)


@pytest.mark.asyncio
async def test_bulk_task_cancellation_cannot_orphan_a_journal_worker(tmp_path):
    import contextvars
    outbox = staged_outbox(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    marker = contextvars.ContextVar("journal_test_marker", default=None)
    marker.set("request-context")
    def write():
        assert marker.get() == "request-context"
        entered.set()
        assert release.wait(3)
        finished.set()
    previous_tasks = asyncio.all_tasks()
    parent = asyncio.create_task(outbox.run_io(write))
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        # Emulate runner shutdown without cancelling pytest's own tasks.
        for task in asyncio.all_tasks() - previous_tasks:
            task.cancel("shutdown")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not parent.done()
        assert not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError, match="shutdown"):
        await parent
    assert finished.is_set()


@pytest.mark.asyncio
async def test_prepare_between_snapshot_and_partial_mark_is_not_lost(tmp_path, monkeypatch):
    outbox = TurnOutbox(str(tmp_path / "outbox.json"))
    outbox.begin("rid", request_message_id=1, channel_id=2, user_id=3)
    outbox.record_delivery("rid", message_ids=[4], streamed=True)
    original = outbox._mark_partial_if_incomplete
    def provider_finishes_before_mark(request_id):
        outbox.prepare(request_id, {"response_text": "complete answer"})
        return original(request_id)
    monkeypatch.setattr(outbox, "_mark_partial_if_incomplete", provider_finishes_before_mark)
    called = []
    async def callback(turn):
        called.append(turn["idempotency_step"])
    assert await outbox.reconcile("rid", archive=callback, session=callback, record=callback)
    assert called == ["session", "record"]
    reloaded = TurnOutbox(outbox.path).snapshot("rid")
    assert reloaded["payload_complete"] is True
    assert "terminal_state" not in reloaded
    assert reloaded["steps"] == {"archive": "done", "session": "done", "record": "done"}

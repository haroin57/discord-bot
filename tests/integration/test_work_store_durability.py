"""Real SQLite reopen, abrupt process exit and independently competing writers."""

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor


def test_acknowledged_work_survives_process_exit_before_execution(tmp_path):
    from services.work.store import WorkStore
    database = tmp_path / "work.sqlite3"
    script = """
import json, os, sys
from services.work.store import WorkStore
s = WorkStore(sys.argv[1])
w = s.create('owner', 'channel', '翌日まで続く調査です。', 'discord-message-1', requirements={'rent': 80000})
s.bind_message(w.id, 'bot-message', 'channel')
s.bind_message(w.id, 'research-anchor', 'research-thread')
q = s.add_question(w.id, '通勤先を指定してください。', request_id='question-1')
s.bind_message(w.id, 'research-question-message', 'research-thread')
print(json.dumps({'work_id': w.id, 'question_id': q['id']}), flush=True)
os._exit(0)
"""
    result = subprocess.run([sys.executable, "-c", script, str(database)], capture_output=True, text=True, check=True)
    ids = json.loads(result.stdout)
    reopened = WorkStore(database)
    work = reopened.resolve("owner", "channel", reply_message_id="bot-message")
    assert work.id == ids["work_id"]
    assert work.state == "queued" and work.requirements == {"rent": 80000}
    assert reopened.list_questions(work.id)[0]["id"] == ids["question_id"]
    assert reopened.list_for("owner", "research-thread") == [work]
    assert reopened.resolve("owner", "research-thread") == work


def test_concurrent_revision_edits_have_one_winner(tmp_path):
    from services.work.store import WorkRevisionConflict, WorkStore
    database = tmp_path / "work.sqlite3"
    first, second = WorkStore(database), WorkStore(database)
    work = first.create("owner", "channel", "比較します。", "create")
    barrier = threading.Barrier(2)

    def change(store, index):
        barrier.wait(timeout=5)
        try:
            return store.amend(work.id, "owner", f"条件{index}", f"amend-{index}", expected_revision=1)
        except WorkRevisionConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(change, store, index) for index, store in enumerate((first, second))]
        results = [future.result(timeout=15) for future in futures]
    assert sum(result is not None for result in results) == 1
    assert first.get(work.id).revision == 2
    assert len(first.get(work.id).requirements["amendments"]) == 1


def test_concurrent_same_source_is_accepted_once(tmp_path):
    from services.work.store import WorkStore
    database = tmp_path / "work.sqlite3"
    stores = [WorkStore(database), WorkStore(database)]
    barrier = threading.Barrier(2)

    def accept(store):
        barrier.wait(timeout=5)
        return store.create("owner", "channel", "比較します。", "same-message")

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(accept, stores))
    assert results[0].id == results[1].id
    assert len(stores[0].list_for("owner")) == 1


def test_effect_claim_transaction_serializes_with_cancel(tmp_path):
    from services.work.store import WorkStore
    database = tmp_path / "work.sqlite3"
    store, other = WorkStore(database), WorkStore(database)
    work = store.create("owner", "channel", "外部操作の用件です。", "create")
    work = store.transition(work.id, "owner", "running")
    attempted = threading.Event()

    def cancel():
        attempted.set()
        return other.transition(work.id, "owner", "cancelled")

    with ThreadPoolExecutor(1) as pool:
        with store.transaction() as connection:
            connection.execute("CREATE TABLE test_effect_claims (work_id TEXT, revision INTEGER)")
            store.assert_current(work.id, work.revision, "owner", allowed_states={"running"})
            future = pool.submit(cancel)
            assert attempted.wait(timeout=5)
            # A separate, read-only WorkStore connection is usable while this writer holds the claim.
            assert other.get(work.id, "owner").state == "running"
            connection.execute("INSERT INTO test_effect_claims VALUES (?,?)", (work.id, work.revision))
            assert not future.done()
        assert future.result(timeout=15).state == "cancelled"
    assert store.get(work.id).state == "cancelled"

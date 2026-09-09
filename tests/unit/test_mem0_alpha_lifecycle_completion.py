"""Transactional history rebuild and deletion graph cleanup, fully offline."""
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
import sqlite3
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest


pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(scope="module", autouse=True)
def bootstrap():
    import services.mem0 as package
    return package


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from llm_mem0 import bm25_index, client, fact_store, graph, history_index, search
    from llm_mem0.sqlite_store import SqliteStore
    monkeypatch.setenv("LLM_MEM0_STATE_DIR", str(tmp_path))
    for module in (history_index, graph, bm25_index, fact_store):
        store = SqliteStore(module._store._filename, module._store._schema)
        store.reset_path_for_test(str(tmp_path / store._filename))
        monkeypatch.setattr(module, "_store", store)
    monkeypatch.setattr(client, "_get_mem0", lambda: None)
    monkeypatch.setattr(search, "invalidate_core_cache", Mock())

    def deny(*args, **kwargs):
        raise AssertionError("Network forbidden in lifecycle completion tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def _seed_history():
    from llm_mem0 import history_index as history
    assert history.index_line(7, "2026-09-01", "user", "Amberfox original", "Alice",
                              author_id="a", source_id="original:user")
    assert history.index_line(7, "2026-09-01", "assistant", "Original reply", source_id="original:assistant")
    history.advance_replay_cursor(7, "a", "2026-09-01", 2)
    return history


def _history_snapshot(history):
    with history._store.connect() as connection:
        return {table: [tuple(row) for row in connection.execute(query).fetchall()]
                for table, query in (
                    ("fts", "SELECT rowid,* FROM history_fts ORDER BY rowid"),
                    ("metadata", "SELECT * FROM history_line_meta ORDER BY row_id"),
                    ("cursors", "SELECT * FROM replay_cursors ORDER BY channel_id,user_id"),
                )}


def test_reindex_generator_failure_preserves_rows_metadata_cursors_and_file():
    history = _seed_history()
    before = _history_snapshot(history)
    inode = os.stat(history._store.resolve_path()).st_ino

    def broken_source():
        yield (7, "2026-09-02", "user", "New first row", "Alice")
        raise RuntimeError("Synthetic source read failure")

    assert history.reindex_all(broken_source()) == 0
    assert _history_snapshot(history) == before
    assert os.stat(history._store.resolve_path()).st_ino == inode
    assert history.search_history("Amberfox", 7)


def test_reindex_tokenization_failure_preserves_existing_state(monkeypatch):
    history = _seed_history()
    before = _history_snapshot(history)
    monkeypatch.setattr(history, "_tokenize", Mock(side_effect=RuntimeError("synthetic tokenize failure")))
    assert history.reindex_all([(7, "later", "user", "new", "Alice")]) == 0
    assert _history_snapshot(history) == before


def test_reindex_sql_failure_rolls_back_deleted_rows_metadata_and_cursors():
    history = _seed_history()
    before = _history_snapshot(history)
    # An unsupported SQLite parameter fails after all three DELETE statements.
    # The first inserted replacement must roll back together with those deletes.
    rows = [(7, "later", "user", "Replacement one", "Alice"),
            (7, "later", "user", "Replacement two", {"invalid_sql_value": True})]
    assert history.reindex_all(rows) == 0
    assert _history_snapshot(history) == before


def test_reindex_success_is_atomic_and_preserves_explicit_identity():
    history = _seed_history()
    inode = os.stat(history._store.resolve_path()).st_ino
    assert history.reindex_all([
        (8, "2026-09-02", "user", "Cedarwing observation", "Bob", "b", "new:user"),
        (8, "2026-09-02", "assistant", "New reply", "", "", "new:assistant"),
    ]) == 2
    assert not history.search_history("Amberfox", 7)
    rows = history.get_lines_since(8)
    assert rows[0]["author_id"] == "b" and rows[0]["source_id"] == "new:user"
    assert rows[1]["author_id"] == "" and rows[1]["source_id"] == "new:assistant"
    assert history.get_replay_cursor(7, "a") == ("", 0)
    assert os.stat(history._store.resolve_path()).st_ino == inode


def test_reindex_legacy_rows_never_inherit_old_row_id_ownership():
    history = _seed_history()
    assert history.reindex_all([(7, "later", "user", "Legacy line"),
                                (7, "later", "user", "Another legacy line", "Alice")]) == 2
    rows = history.get_lines_since(7)
    assert all(row["author_id"] == "" and row["source_id"] == "" for row in rows)
    assert history.get_replay_cursor(7, "a") == ("", 0)


def test_reindex_empty_input_explicitly_clears_all_three_tables():
    history = _seed_history()
    assert history.reindex_all([]) == 0
    assert _history_snapshot(history) == {"fts": [], "metadata": [], "cursors": []}


def test_reindex_lock_retry_reuses_prepared_generator(monkeypatch):
    history = _seed_history()
    original_write = history._store.write
    attempts = 0
    source_iterations = []

    def source():
        for number in range(3):
            source_iterations.append(number)
            yield (8, "later", "user", f"Replacement {number}", "Bob", "b", str(number))

    def retry_after_insert(fn, **kwargs):
        def attempt(connection):
            nonlocal attempts
            attempts += 1
            result = fn(connection)
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return result
        return original_write(attempt, **kwargs)

    monkeypatch.setattr(history._store, "write", retry_after_insert)
    assert history.reindex_all(source()) == 3
    assert attempts == 2 and source_iterations == [0, 1, 2]
    assert len(history.get_lines_since(8)) == 3


def test_reindex_readers_never_observe_partial_replacement(monkeypatch):
    history = _seed_history()
    before = _history_snapshot(history)
    deleted, release = threading.Event(), threading.Event()
    original_write = history._store.write

    class PausingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            result = self.connection.execute(sql, *args)
            if sql == "DELETE FROM history_fts":
                deleted.set()
                assert release.wait(3)
            return result

    def paused_write(fn, **kwargs):
        return original_write(lambda connection: fn(PausingConnection(connection)), **kwargs)

    monkeypatch.setattr(history._store, "write", paused_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(history.reindex_all, [(8, "later", "user", "Replacement", "Bob")])
        try:
            assert deleted.wait(3)
            # Independent connection bypasses the module's in-process write lock
            # and exercises actual SQLite WAL visibility while DELETE is pending.
            with sqlite3.connect(f"file:{history._store.resolve_path()}?mode=ro", uri=True) as connection:
                rows = [tuple(row) for row in connection.execute("SELECT rowid,* FROM history_fts ORDER BY rowid")]
                assert rows == before["fts"]
        finally:
            release.set()
        assert future.result(timeout=3) == 1
    assert len(history.get_lines_since(8)) == 1


def _seed_relations():
    from llm_mem0 import graph
    alice = graph.upsert_entity("Alice", "person", "a")
    project = graph.upsert_entity("Amberfox", "topic", "a")
    place = graph.upsert_entity("Cedarwing", "place", "a")
    assert alice and project and place
    target = graph.add_relation(alice, "knows", project, "a", source_fact_id="delete-me")
    historical = graph.add_relation(alice, "loves", project, "a", source_fact_id="delete-me")
    assert graph.expire_relation(historical, valid_to=100)
    unrelated = graph.add_relation(alice, "knows", place, "a", source_fact_id="keep-me")
    assert target and historical and unrelated
    return NS(graph=graph, entity=alice, target=target, historical=historical, unrelated=unrelated)


@pytest.mark.asyncio
async def test_public_delete_cleans_only_target_sourced_relations(monkeypatch):
    from llm_mem0 import client, search
    data = _seed_relations()
    provider = NS(delete=Mock())
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    expiry = Mock(wraps=data.graph.expire_relations_for_fact)
    monkeypatch.setattr(data.graph, "expire_relations_for_fact", expiry)
    assert await client.delete_memory("delete-me")
    assert provider.delete.call_args.args == ("delete-me",)
    assert search.invalidate_core_cache.called
    relations = {row.id: row for row in data.graph.get_relations(data.entity, only_valid=False)}
    # The current graph delete primitive physically removes sourced relations;
    # expiry remains an independent cleanup for implementations/failures that do
    # not. Neither route may modify unrelated provenance.
    assert data.target not in relations and data.historical not in relations
    assert relations[data.unrelated].valid_to is None
    expiry.assert_called_once_with("delete-me")
    assert await client.delete_memory("delete-me")
    again = {row.id: row for row in data.graph.get_relations(data.entity, only_valid=False)}
    assert again == relations
    assert expiry.call_count == 2


@pytest.mark.asyncio
async def test_relation_expiry_retains_previous_temporal_boundaries(monkeypatch):
    from llm_mem0 import client
    data = _seed_relations()
    monkeypatch.setattr(client, "_get_mem0", lambda: NS(delete=Mock()))
    monkeypatch.setattr(data.graph, "delete_fact_edges", lambda _: 0)
    assert await client.delete_memory("delete-me")
    rows = {row.id: row for row in data.graph.get_relations(data.entity, only_valid=False)}
    assert rows[data.target].valid_to is not None
    assert rows[data.historical].valid_to == 100
    assert rows[data.unrelated].valid_to is None
    assert await client.delete_memory("delete-me")
    again = {row.id: row for row in data.graph.get_relations(data.entity, only_valid=False)}
    assert again[data.target].valid_to == rows[data.target].valid_to


@pytest.mark.asyncio
async def test_failed_edge_cleanup_does_not_skip_relation_expiry_or_other_mirrors(monkeypatch):
    from llm_mem0 import client, bm25_index, fact_store
    data = _seed_relations()
    monkeypatch.setattr(client, "_get_mem0", lambda: NS(delete=Mock()))
    monkeypatch.setattr(data.graph, "delete_fact_edges", Mock(side_effect=RuntimeError("synthetic edge failure")))
    monkeypatch.setattr(bm25_index, "remove_fact", Mock())
    monkeypatch.setattr(fact_store, "remove_fact", Mock())
    assert await client.delete_memory("delete-me")
    assert data.target not in {row.id for row in data.graph.get_relations(data.entity)}
    bm25_index.remove_fact.assert_called_once_with("delete-me")
    fact_store.remove_fact.assert_called_once_with("delete-me")


@pytest.mark.asyncio
async def test_failed_provider_delete_preserves_relations(monkeypatch):
    from llm_mem0 import client
    data = _seed_relations()
    monkeypatch.setattr(client, "_get_mem0", lambda: NS(delete=Mock(side_effect=RuntimeError("synthetic provider failure"))))
    assert not await client.delete_memory("delete-me")
    assert data.target in {row.id for row in data.graph.get_relations(data.entity)}


@pytest.mark.asyncio
async def test_cancelled_awaiter_still_finishes_relation_cleanup_in_same_worker(monkeypatch):
    from llm_mem0 import client, search
    data = _seed_relations()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delete(_):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(client, "_get_mem0", lambda: NS(delete=delete))
    monkeypatch.setattr(search, "invalidate_core_cache", lambda: finished.set())
    task = asyncio.create_task(client.delete_memory("delete-me"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
    assert data.target not in {row.id for row in data.graph.get_relations(data.entity)}


class ReceiptMemory:
    """Authoritative fake rows; timeouts can follow a real state mutation."""

    def __init__(self):
        self.rows = {}
        self.add_calls = 0
        self.delete_calls = 0
        self.insert_timeout = False
        self.delete_error = None
        self.delete_before_error = False
        self.before_delete = lambda: None

    def add(self, messages, *, user_id, metadata, infer):
        assert infer is False
        self.add_calls += 1
        fid = f"receipt-fact-{self.add_calls}"
        self.rows[fid] = {"id": fid, "user_id": user_id,
                          "memory": messages[0]["content"], "metadata": deepcopy(metadata)}
        if self.insert_timeout:
            self.insert_timeout = False
            raise TimeoutError("synthetic timeout after primary commit")
        return {"results": [{"id": fid}]}

    def get(self, memory_id):
        return deepcopy(self.rows.get(memory_id))

    def get_all(self, user_id=None, filters=None, limit=None):
        owner = user_id or (filters or {}).get("user_id")
        return {"results": [deepcopy(row) for row in self.rows.values() if row["user_id"] == owner][:limit]}

    def delete(self, memory_id):
        self.delete_calls += 1
        self.before_delete()
        if not self.delete_error or self.delete_before_error:
            self.rows.pop(memory_id, None)
        if self.delete_error:
            raise self.delete_error


def _receipt_contents(tmp_path, key):
    path = tmp_path / "mem0_ingest_receipts.sqlite3"
    if not path.exists():
        return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT response FROM receipts WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _receipt_insert_with_uncertain_commit(tmp_path):
    from llm_mem0 import ingest
    provider = ReceiptMemory()
    provider.insert_timeout = True
    fact = {"text": "Synthetic preference for Amberfox", "importance": 3,
            "category": "personal", "scope": "global"}
    meta = ingest._build_fact_meta(fact, {"request_id": "original-source-event"})
    with pytest.raises(TimeoutError):
        ingest._insert_raw_sync(provider, "u", fact, meta)
    fid, row = next(iter(provider.rows.items()))
    key = row["metadata"]["ingest_key"]
    assert json.loads(_receipt_contents(tmp_path, key)) == {"pending": True}
    return provider, fid, key, fact, meta


@pytest.mark.asyncio
async def test_delete_pending_receipt_prevents_same_source_resurrection(monkeypatch, tmp_path):
    from llm_mem0 import client, ingest
    provider, fid, key, fact, meta = _receipt_insert_with_uncertain_commit(tmp_path)
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)

    def verify_tombstone_precedes_provider_delete():
        assert json.loads(_receipt_contents(tmp_path, key)) == {"deleted": True, "results": []}

    provider.before_delete = verify_tombstone_precedes_provider_delete
    assert await client.delete_memory(fid)
    retry = await asyncio.to_thread(ingest._insert_raw_sync, provider, "u", fact, meta)
    assert retry == {"error": "receipt_target_deleted", "results": []}
    assert provider.rows == {} and provider.add_calls == 1
    assert await client.delete_memory(fid)
    assert json.loads(_receipt_contents(tmp_path, key))["deleted"] is True


@pytest.mark.parametrize("previous", [None, '{ "pending": true }', '{"results":[{"id":"f"}]}'])
@pytest.mark.asyncio
async def test_confirmed_delete_failure_restores_exact_previous_receipt(monkeypatch, tmp_path, previous):
    from llm_mem0 import client
    provider = ReceiptMemory()
    provider.rows["f"] = {"id": "f", "user_id": "u", "memory": "Synthetic fact",
                          "metadata": {"ingest_key": "synthetic-key"}}
    path = tmp_path / "mem0_ingest_receipts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE receipts (key TEXT PRIMARY KEY, response TEXT NOT NULL)")
        if previous is not None:
            connection.execute("INSERT INTO receipts VALUES (?, ?)", ("synthetic-key", previous))
    provider.delete_error = RuntimeError("synthetic rejected deletion")
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    assert not await client.delete_memory("f")
    assert _receipt_contents(tmp_path, "synthetic-key") == previous
    assert "f" in provider.rows


@pytest.mark.asyncio
async def test_delete_commit_then_timeout_retains_tombstone(monkeypatch, tmp_path):
    from llm_mem0 import client, ingest
    provider, fid, key, fact, meta = _receipt_insert_with_uncertain_commit(tmp_path)
    provider.delete_before_error = True
    provider.delete_error = TimeoutError("synthetic timeout after delete commit")
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    assert not await client.delete_memory(fid)
    assert json.loads(_receipt_contents(tmp_path, key))["deleted"] is True
    retry = await asyncio.to_thread(ingest._insert_raw_sync, provider, "u", fact, meta)
    assert retry["error"] == "receipt_target_deleted"
    assert provider.rows == {} and provider.add_calls == 1


@pytest.mark.asyncio
async def test_uncertain_delete_readback_keeps_tombstone(monkeypatch, tmp_path):
    from llm_mem0 import client
    provider, fid, key, _, _ = _receipt_insert_with_uncertain_commit(tmp_path)
    provider.delete_error = TimeoutError("synthetic uncertain deletion")
    original_get = provider.get

    def get(memory_id):
        if provider.delete_calls:
            raise TimeoutError("synthetic unavailable readback")
        return original_get(memory_id)

    provider.get = get
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    assert not await client.delete_memory(fid)
    assert json.loads(_receipt_contents(tmp_path, key))["deleted"] is True


def test_crash_after_tombstone_does_not_restore_receipt(tmp_path):
    from llm_mem0 import client
    provider, fid, key, _, _ = _receipt_insert_with_uncertain_commit(tmp_path)
    provider.delete_error = SystemExit("synthetic process stop")
    with pytest.raises(SystemExit):
        client._delete_memory_sync(provider, fid)
    assert json.loads(_receipt_contents(tmp_path, key))["deleted"] is True
    assert fid in provider.rows  # retry is suppressed even before provider mutation


@pytest.mark.asyncio
async def test_tombstone_fsync_failure_prevents_provider_delete(monkeypatch, tmp_path):
    from llm_mem0 import client
    provider, fid, key, _, _ = _receipt_insert_with_uncertain_commit(tmp_path)
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    monkeypatch.setattr(client.os, "fsync", Mock(side_effect=OSError("synthetic fsync failure")))
    assert not await client.delete_memory(fid)
    assert provider.delete_calls == 0 and fid in provider.rows
    assert json.loads(_receipt_contents(tmp_path, key))["deleted"] is True


@pytest.mark.asyncio
async def test_cancelled_delete_waiter_still_commits_tombstone_and_deletes(monkeypatch, tmp_path):
    from llm_mem0 import client
    provider, fid, key, _, _ = _receipt_insert_with_uncertain_commit(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def before_delete():
        entered.set()
        assert release.wait(3)

    provider.before_delete = before_delete
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    monkeypatch.setattr("llm_mem0.search.invalidate_core_cache", lambda: finished.set())
    task = asyncio.create_task(client.delete_memory(fid))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert json.loads(_receipt_contents(tmp_path, key))["deleted"] is True
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
    assert provider.rows == {}


def test_retry_waits_for_inflight_delete_tombstone(tmp_path):
    from llm_mem0 import client, ingest
    provider, fid, key, fact, meta = _receipt_insert_with_uncertain_commit(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def before_delete():
        entered.set()
        assert release.wait(3)

    provider.before_delete = before_delete
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(client._delete_memory_sync, provider, fid)
        try:
            assert entered.wait(3)
            retry = pool.submit(ingest._insert_raw_sync, provider, "u", fact, meta)
            assert not retry.done()
        finally:
            release.set()
        assert deleting.result(timeout=3)
        assert retry.result(timeout=3)["error"] == "receipt_target_deleted"
    assert provider.rows == {} and provider.add_calls == 1


def test_delete_waits_for_pending_receipt_reconciliation(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from llm_mem0 import client, ingest
    provider, fid, key, fact, meta = _receipt_insert_with_uncertain_commit(tmp_path)
    observed, release, delete_lock_attempt = threading.Event(), threading.Event(), threading.Event()
    original_get_all = provider.get_all
    original_receipt_lock = client._ingest_receipt_lock

    def paused_get_all(*args, **kwargs):
        result = original_get_all(*args, **kwargs)
        observed.set()  # retry has read the primary but not committed its receipt
        assert release.wait(3)
        return result

    @contextmanager
    def observed_receipt_lock(receipt_key):
        if observed.is_set():
            delete_lock_attempt.set()
        with original_receipt_lock(receipt_key):
            yield

    provider.get_all = paused_get_all
    monkeypatch.setattr(client, "_ingest_receipt_lock", observed_receipt_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        retry = pool.submit(ingest._insert_raw_sync, provider, "u", fact, meta)
        try:
            assert observed.wait(3)
            deleting = pool.submit(client._delete_memory_sync, provider, fid)
            assert delete_lock_attempt.wait(3)
            assert provider.delete_calls == 0
        finally:
            release.set()
        assert retry.result(timeout=3)["results"][0]["id"] == fid
        assert deleting.result(timeout=3)
    assert json.loads(_receipt_contents(tmp_path, key))["deleted"] is True
    assert provider.rows == {} and provider.add_calls == 1


@pytest.mark.asyncio
async def test_failed_primary_read_prevents_delete_and_preserves_receipt(monkeypatch, tmp_path):
    from llm_mem0 import client
    provider, fid, key, _, _ = _receipt_insert_with_uncertain_commit(tmp_path)
    before = _receipt_contents(tmp_path, key)
    provider.get = Mock(side_effect=TimeoutError("synthetic primary read failure"))
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    assert not await client.delete_memory(fid)
    assert provider.delete_calls == 0 and fid in provider.rows
    assert _receipt_contents(tmp_path, key) == before


@pytest.mark.asyncio
async def test_failed_receipt_restore_retains_tombstone(monkeypatch, tmp_path):
    from llm_mem0 import client
    provider, fid, key, _, _ = _receipt_insert_with_uncertain_commit(tmp_path)
    provider.delete_error = RuntimeError("synthetic rejected deletion")
    monkeypatch.setattr(client, "_get_mem0", lambda: provider)
    monkeypatch.setattr(client, "_restore_deleted_receipt", Mock(side_effect=OSError("synthetic restore failure")))
    assert not await client.delete_memory(fid)
    assert fid in provider.rows
    assert json.loads(_receipt_contents(tmp_path, key))["deleted"] is True

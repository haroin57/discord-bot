"""Phase 2 Mem0 canonical ledger, outbox, and durable queue tests."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from services.mem0.integrity import (
    CanonicalRepository,
    LegacyProjectionHandlers,
    MemoryApplicationService,
    ProjectionWorker,
    canonical_fact_visible,
    ensure_projection_consumer,
    get_repository,
    reset_repository_cache_for_tests,
    shutdown_projection_consumers,
)


class FakeMem0:
    def __init__(
        self,
        *,
        backend: str = "fake",
        endpoint: str = "memory://mem0",
        collection: str = "default",
    ):
        self.rows: dict[str, dict] = {}
        self.add_calls = 0
        self.get_all_calls = 0
        self.update_calls = 0
        self.delete_calls = 0
        self.fail_after_add_commit = False
        self.storage_identity = {
            "backend": backend,
            "endpoint": endpoint,
            "collection": collection,
        }

    def add(self, messages, *, user_id, metadata, infer):
        assert infer is False
        self.add_calls += 1
        memory_id = f"legacy-{self.add_calls}"
        self.rows[memory_id] = {
            "id": memory_id,
            "memory": messages[0]["content"],
            "user_id": str(user_id),
            "metadata": dict(metadata),
        }
        if self.fail_after_add_commit:
            raise RuntimeError("provider committed but response was lost")
        return {"results": [{"id": memory_id, "memory": messages[0]["content"]}]}

    def get(self, memory_id):
        return self.rows.get(memory_id)

    def get_all(self, *, user_id, filters=None, limit=None):
        self.get_all_calls += 1
        rows = [row for row in self.rows.values() if row["user_id"] == str(user_id)]
        if filters:
            rows = [
                row
                for row in rows
                if all(
                    (row.get("metadata") or {}).get(k) == v for k, v in filters.items()
                )
            ]
        if limit is not None:
            rows = rows[:limit]
        return {"results": rows}

    def update(self, *, memory_id, data, metadata):
        self.update_calls += 1
        row = self.rows[memory_id]
        row["memory"] = data
        row["metadata"].update(metadata)

    def delete(self, memory_id):
        self.delete_calls += 1
        self.rows.pop(memory_id, None)


@pytest.fixture()
def repository(tmp_path):
    return CanonicalRepository(tmp_path / "canonical.sqlite")


@pytest.fixture()
def projection_paths(monkeypatch, tmp_path):
    from services.mem0 import bm25_index, fact_store, graph

    monkeypatch.setattr(bm25_index, "_db_path", str(tmp_path / "bm25.sqlite"))
    monkeypatch.setattr(bm25_index, "_wal_ready_path", None)
    monkeypatch.setattr(graph, "_db_path", str(tmp_path / "graph.sqlite"))
    monkeypatch.setattr(graph, "_wal_ready_path", None)
    monkeypatch.setattr(fact_store, "_db_path", str(tmp_path / "facts.sqlite"))
    monkeypatch.setattr(fact_store, "_wal_ready_path", None)
    graph.invalidate_top_entities_cache()
    return bm25_index, graph, fact_store


class TestCanonicalTransaction:
    def test_fact_and_projection_intents_commit_together(self, repository):
        fact = repository.create_fact(
            "u1",
            "Rustが好き",
            {"attribute": "preference.language"},
            entities=[{"name": "Rust", "type": "tech"}],
            fact_id="c1",
        )

        assert fact["status"] == "active"
        assert fact["metadata"]["canonical_id"] == "c1"
        rows = repository.outbox_rows(aggregate_id="c1")
        assert [row["projection"] for row in rows] == [
            "vector",
            "bm25",
            "graph",
            "fact_store",
        ]
        assert all(row["status"] == "pending" for row in rows)
        assert rows[1]["depends_on_id"] == rows[0]["id"]

    def test_outbox_insert_failure_rolls_back_canonical_write(self, repository):
        with repository._tx() as conn:
            conn.execute(
                """CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox
                   BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END""",
            )

        with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
            repository.create_fact("u1", "must rollback", {}, fact_id="rollback")

        assert repository.get_fact("rollback") is None
        assert repository.outbox_rows(aggregate_id="rollback") == []

    def test_consolidate_creates_and_archives_in_one_transaction(self, repository):
        repository.create_fact(
            "u1",
            "old one",
            {},
            fact_id="old-1",
            project_vector=False,
        )
        repository.create_fact(
            "u1",
            "old two",
            {},
            fact_id="old-2",
            project_vector=False,
        )

        created = repository.consolidate(
            "u1",
            ["old-1", "old-2"],
            [{"fact_id": "merged", "text": "richer fact", "metadata": {}}],
        )

        assert [fact["fact_id"] for fact in created] == ["merged"]
        assert repository.get_fact("merged")["status"] == "active"
        assert repository.get_fact("old-1")["status"] == "archived"
        assert repository.get_fact("old-2")["merged_into"] == "merged"
        assert repository.outbox_rows(aggregate_id="merged")
        assert repository.outbox_rows(aggregate_id="old-1")


class TestProjectionDelivery:
    def test_later_projection_version_waits_for_earlier_version(self, repository):
        repository.create_fact("u1", "fact", {}, fact_id="ordered")
        repository.archive_fact("ordered", reason="stale")

        leases = repository.lease_outbox("worker", limit=20)

        assert [(lease.operation, lease.projection) for lease in leases] == [
            ("upsert", "vector"),
        ]

    def test_expired_outbox_lease_is_fenced_even_with_same_owner(self, repository):
        repository.create_fact("u1", "fact", {}, fact_id="fenced")
        first = repository.lease_outbox("same", lease_seconds=0.01)[0]
        time.sleep(0.02)
        second = repository.lease_outbox("same")[0]

        assert first.id == second.id
        assert first.token != second.token
        assert not repository.ack_outbox(first, "mem0-projection")
        assert repository.retry_outbox(first, "stale") == "lost"
        assert repository.ack_outbox(second, "mem0-projection")

    def test_expired_outbox_lease_cannot_ack_or_advance_cursor(self, repository):
        repository.create_fact("u1", "fact", {}, fact_id="expired-ack")
        lease = repository.lease_outbox("worker", lease_seconds=0.03)[0]
        ack_results = []
        with repository._write_lock:
            thread = threading.Thread(
                target=lambda: ack_results.append(
                    repository.ack_outbox(lease, "mem0-projection"),
                ),
            )
            thread.start()
            time.sleep(0.05)
        thread.join(1)

        assert ack_results == [False]
        assert repository.retry_outbox(lease, "expired") == "lost"
        assert repository.projection_cursor() == 0
        assert (
            repository.outbox_rows(aggregate_id="expired-ack")[0]["status"] == "leased"
        )

    def test_projection_heartbeat_prevents_reclaim_during_slow_handler(
        self,
        repository,
    ):
        repository.create_fact(
            "u1",
            "fact",
            {},
            fact_id="heartbeat",
            project_vector=False,
        )
        entered = threading.Event()
        release = threading.Event()
        results = []
        handled_ids = []

        def slow_handler(lease):
            handled_ids.append(lease.id)
            entered.set()
            assert release.wait(1)

        worker = ProjectionWorker(
            repository,
            {"bm25": slow_handler},
            retry_seconds=0,
            lease_seconds=0.03,
            heartbeat_interval=0.005,
        )
        thread = threading.Thread(
            target=lambda: results.append(worker.run_once(limit=1))
        )
        thread.start()
        try:
            assert entered.wait(1)
            time.sleep(0.08)
            competing = repository.lease_outbox("competing-worker", limit=20)
            assert handled_ids[0] not in {lease.id for lease in competing}
        finally:
            release.set()
            thread.join(1)

        assert not thread.is_alive()
        assert results[0]["acked"] == 1

    def test_expired_old_version_cannot_rollback_new_provider_metadata(
        self,
        repository,
    ):
        mem = FakeMem0()
        mem.rows["legacy-versioned"] = {
            "id": "legacy-versioned",
            "memory": "fact",
            "user_id": "u1",
            "metadata": {"canonical_id": "versioned", "mention_count": 1},
        }
        repository.create_fact(
            "u1",
            "fact",
            {"mention_count": 1},
            fact_id="versioned",
            legacy_id="legacy-versioned",
        )
        handlers = LegacyProjectionHandlers(repository, mem)
        worker = ProjectionWorker(
            repository,
            {
                **handlers.as_dict(),
                "bm25": lambda _lease: None,
                "graph": lambda _lease: None,
                "fact_store": lambda _lease: None,
            },
            retry_seconds=0,
        )
        worker.drain(aggregate_id="versioned")

        repository.patch_metadata("versioned", {"mention_count": 2})
        stale_v2 = repository.lease_outbox(
            "stale-v2",
            aggregate_id="versioned",
            lease_seconds=0.01,
        )[0]
        time.sleep(0.02)
        assert worker.run_once(aggregate_id="versioned")["acked"] == 1
        repository.patch_metadata("versioned", {"mention_count": 3})
        assert worker.run_once(aggregate_id="versioned")["acked"] == 1
        assert mem.rows["legacy-versioned"]["metadata"]["mention_count"] == 3

        stale_worker = ProjectionWorker(
            repository,
            handlers.as_dict(),
            retry_seconds=0,
        )
        assert stale_worker._process_lease(stale_v2) == "lost"
        assert mem.update_calls == 2
        assert mem.rows["legacy-versioned"]["metadata"]["mention_count"] == 3

    def test_retry_ack_and_processed_cursor_are_contiguous(self, repository):
        repository.create_fact("u1", "fact", {}, fact_id="c1")
        failures = {"bm25": 1}

        def handler(name):
            def apply(_lease):
                if failures.get(name, 0):
                    failures[name] -= 1
                    raise RuntimeError("injected projection failure")

            return apply

        worker = ProjectionWorker(
            repository,
            {name: handler(name) for name in ("vector", "bm25", "graph", "fact_store")},
            retry_seconds=0,
        )

        first = worker.drain()
        assert first["retried"] == 1
        assert repository.projection_cursor() == 1

        second = worker.drain()
        assert second["acked"] == 1
        rows = repository.outbox_rows()
        assert all(row["status"] == "acked" for row in rows)
        assert repository.projection_cursor() == rows[-1]["id"]

    def test_handlers_receive_immutable_event_payload(self, repository):
        repository.create_fact("u1", "original", {}, fact_id="immutable")
        with repository._tx() as conn:
            conn.execute(
                "UPDATE canonical_facts SET text='mutated without event' WHERE fact_id=?",
                ("immutable",),
            )
        observed = []
        worker = ProjectionWorker(
            repository,
            {
                name: lambda lease: observed.append(lease.payload["text"])
                for name in ("vector", "bm25", "graph", "fact_store")
            },
            retry_seconds=0,
        )

        worker.drain(aggregate_id="immutable")

        assert observed == ["original", "original", "original", "original"]

    def test_watermark_rejects_old_upsert_after_archive(self, repository):
        repository.create_fact("u1", "fact", {}, fact_id="watermarked")
        repository.archive_fact("watermarked", reason="stale")
        calls = []
        worker = ProjectionWorker(
            repository,
            {
                name: lambda lease: calls.append((lease.version, lease.operation, name))
                for name in ("vector", "bm25", "graph", "fact_store")
            },
            retry_seconds=0,
        )
        worker.drain(aggregate_id="watermarked")
        assert repository.projection_watermark("watermarked", "vector") == 2

        vector_v1 = next(
            row
            for row in repository.outbox_rows(aggregate_id="watermarked")
            if row["projection"] == "vector" and row["version"] == 1
        )
        with repository._tx() as conn:
            conn.execute(
                "UPDATE outbox SET status='pending',acked_at=NULL WHERE id=?",
                (vector_v1["id"],),
            )
        calls.clear()

        assert worker.run_once(aggregate_id="watermarked")["leased"] == 0
        assert calls == []
        stale = next(
            row
            for row in repository.outbox_rows(aggregate_id="watermarked")
            if row["id"] == vector_v1["id"]
        )
        assert stale["status"] == "acked"
        assert stale["last_error"] == "stale version rejected"

    def test_repeated_failure_dead_letters_event_and_dependents(self, repository):
        repository.create_fact("u1", "fact", {}, fact_id="dead")

        def fail(_lease):
            raise RuntimeError("permanent projection failure")

        worker = ProjectionWorker(
            repository,
            {"vector": fail, "bm25": fail, "graph": fail, "fact_store": fail},
            retry_seconds=0,
        )
        for _ in range(5):
            worker.run_once()

        rows = repository.outbox_rows(aggregate_id="dead")
        assert all(row["status"] == "dead" for row in rows)
        assert len(repository.dead_letters("outbox")) == 4
        assert repository.projection_cursor() == rows[-1]["id"]

    def test_provider_commit_response_loss_is_idempotently_recovered(
        self,
        repository,
        projection_paths,
    ):
        mem = FakeMem0()
        mem.fail_after_add_commit = True
        service = MemoryApplicationService(repository, mode="canonical")

        result = service.create_fact(
            mem,
            user_id="u1",
            text="durable fact",
            metadata={"attribute": "profile.fact"},
            entities=[{"name": "Durable", "type": "concept"}],
        )
        canonical_id = result["canonical_id"]
        assert result["projection_pending"] is True
        assert repository.get_fact(canonical_id)["legacy_id"] is None
        assert mem.add_calls == 1

        mem.fail_after_add_commit = False
        handlers = LegacyProjectionHandlers(repository, mem)
        worker = ProjectionWorker(
            repository,
            handlers.as_dict(),
            retry_seconds=0,
        )
        outcome = worker.drain(aggregate_id=canonical_id)

        assert outcome["retried"] == 0
        assert mem.add_calls == 1
        fact = repository.get_fact(canonical_id)
        assert fact["legacy_id"] == "legacy-1"
        assert all(
            row["status"] == "acked"
            for row in repository.outbox_rows(aggregate_id=canonical_id)
        )

    def test_response_loss_retry_fails_closed_when_idempotency_lookup_fails(
        self,
        monkeypatch,
        repository,
        projection_paths,
    ):
        mem = FakeMem0()
        mem.fail_after_add_commit = True
        service = MemoryApplicationService(repository, mode="canonical")
        result = service.create_fact(
            mem,
            user_id="u1",
            text="provider committed",
            metadata={"attribute": "profile.fact"},
        )
        canonical_id = result["canonical_id"]
        assert mem.add_calls == 1
        assert len(mem.rows) == 1

        mem.fail_after_add_commit = False

        def unavailable_lookup(**_kwargs):
            raise TypeError("filters are unavailable")

        monkeypatch.setattr(mem, "get_all", unavailable_lookup)
        worker = ProjectionWorker(
            repository,
            LegacyProjectionHandlers(repository, mem).as_dict(),
            retry_seconds=0,
        )
        outcome = worker.run_once(aggregate_id=canonical_id)

        assert outcome["retried"] == 1
        assert mem.add_calls == 1
        assert len(mem.rows) == 1
        assert repository.get_fact(canonical_id)["legacy_id"] is None

    @pytest.mark.parametrize(
        "lookup_result",
        [
            None,
            {"error": "provider unavailable"},
            {"results": None},
            {"unexpected": []},
            "unknown",
            {"results": [None]},
            {"results": [{"id": "legacy", "metadata": "invalid"}]},
            {
                "results": [
                    {
                        "id": "matching",
                        "metadata": {"canonical_id": "shape-closed"},
                    },
                    None,
                ],
            },
        ],
        ids=[
            "none",
            "error-dict",
            "none-results",
            "unknown-dict",
            "unknown-scalar",
            "invalid-row",
            "invalid-metadata",
            "valid-prefix-then-invalid-row",
        ],
    )
    def test_idempotency_lookup_malformed_shapes_never_add(
        self,
        monkeypatch,
        repository,
        lookup_result,
    ):
        mem = FakeMem0()
        repository.create_fact("u1", "fact", {}, fact_id="shape-closed")
        monkeypatch.setattr(mem, "get_all", lambda **_kwargs: lookup_result)
        worker = ProjectionWorker(
            repository,
            LegacyProjectionHandlers(repository, mem).as_dict(),
            retry_seconds=0,
        )

        outcome = worker.run_once(aggregate_id="shape-closed")

        assert outcome["retried"] == 1
        assert mem.add_calls == 0
        assert repository.get_fact("shape-closed")["legacy_id"] is None

    def test_idempotency_lookup_filters_ignored_binds_only_exact_metadata(
        self,
        monkeypatch,
        repository,
    ):
        mem = FakeMem0()
        rows = [
            {
                "id": "unrelated",
                "memory": "other",
                "metadata": {"canonical_id": "different"},
            },
            {
                "id": "exact",
                "memory": "fact",
                "metadata": {"canonical_id": "filter-target"},
            },
        ]
        monkeypatch.setattr(
            mem,
            "get_all",
            lambda **_kwargs: {"results": rows},
        )
        repository.create_fact("u1", "fact", {}, fact_id="filter-target")
        worker = ProjectionWorker(
            repository,
            LegacyProjectionHandlers(repository, mem).as_dict(),
            retry_seconds=0,
        )

        outcome = worker.run_once(aggregate_id="filter-target")

        assert outcome["acked"] == 1
        assert mem.add_calls == 0
        assert repository.get_fact("filter-target")["legacy_id"] == "exact"

    def test_idempotency_lookup_verified_zero_matches_permits_add(
        self,
        monkeypatch,
        repository,
    ):
        mem = FakeMem0()
        unrelated = {
            "id": "unrelated",
            "memory": "other",
            "metadata": {"canonical_id": "different"},
        }
        original_add = mem.add
        monkeypatch.setattr(
            mem,
            "get_all",
            lambda **_kwargs: {"results": [unrelated]},
        )
        monkeypatch.setattr(mem, "add", original_add)
        repository.create_fact("u1", "new fact", {}, fact_id="new-target")
        worker = ProjectionWorker(
            repository,
            LegacyProjectionHandlers(repository, mem).as_dict(),
            retry_seconds=0,
        )

        outcome = worker.run_once(aggregate_id="new-target")

        assert outcome["acked"] == 1
        assert mem.add_calls == 1
        assert repository.get_fact("new-target")["legacy_id"] == "legacy-1"

    def test_graph_projection_receipt_prevents_mention_inflation(
        self,
        projection_paths,
    ):
        _, graph, _ = projection_paths
        entities = [{"name": "Rust", "type": "tech"}]

        assert graph.apply_fact_projection("event-1", "fact-1", "u1", entities)
        assert graph.apply_fact_projection("event-1", "fact-1", "u1", entities)

        entity = graph.find_entity("Rust", "u1")
        assert entity is not None
        assert entity.mention_count == 1
        assert [item.id for item in graph.get_fact_entities("fact-1")] == [entity.id]


class TestApplicationLifecycle:
    def test_legacy_coverage_schema_migrates_without_trusting_old_cache(
        self,
        tmp_path,
    ):
        path = tmp_path / "legacy-coverage.sqlite"
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE migration_coverage (
                       user_id TEXT PRIMARY KEY,
                       provider_count INTEGER NOT NULL,
                       canonical_count INTEGER NOT NULL,
                       provider_hash TEXT NOT NULL,
                       verified_at REAL NOT NULL
                   )""",
            )
            conn.execute(
                "INSERT INTO migration_coverage VALUES ('u1', 0, 0, 'old', 1)",
            )

        repository = CanonicalRepository(path)
        mem = FakeMem0(collection="current")
        service = MemoryApplicationService(repository, mode="canonical")
        service._ensure_canonical_ready(mem, "u1")

        assert mem.get_all_calls == 1
        with repository._read() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(migration_coverage)")
            }
            identities = conn.execute(
                """SELECT provider_backend,provider_endpoint,provider_collection
                   FROM migration_coverage ORDER BY provider_backend""",
            ).fetchall()
        assert {
            "provider_backend",
            "provider_endpoint",
            "provider_collection",
        } <= columns
        assert [tuple(row) for row in identities] == [
            ("", "", ""),
            ("fake", "memory://mem0", "current"),
        ]

    def test_canonical_mode_rejects_provider_first_inference(self, repository):
        class InferenceMem0(FakeMem0):
            def add(self, messages, *, user_id, metadata):
                self.add_calls += 1
                return {"results": []}

        mem = InferenceMem0()
        service = MemoryApplicationService(repository, mode="canonical")

        with pytest.raises(RuntimeError, match="disabled in canonical mode"):
            service.ingest_legacy_messages(
                mem,
                [{"role": "user", "content": "raw"}],
                user_id="u1",
                metadata={},
            )

        assert mem.add_calls == 0

    def test_canonical_cutover_fails_when_provider_row_is_not_migrated(
        self,
        repository,
    ):
        mem = FakeMem0()
        mem.rows["legacy-only"] = {
            "id": "legacy-only",
            "memory": "unmigrated",
            "user_id": "u1",
            "metadata": {},
        }
        service = MemoryApplicationService(repository, mode="canonical")

        with pytest.raises(RuntimeError, match="coverage missing"):
            service.create_fact(
                mem,
                user_id="u1",
                text="must not write",
                metadata={},
            )

        assert mem.add_calls == 0
        assert repository.list_facts("u1", include_inactive=True) == []

    def test_shadow_backfill_records_verified_cutover_watermark(
        self,
        repository,
        projection_paths,
    ):
        mem = FakeMem0()
        mem.rows["legacy-1"] = {
            "id": "legacy-1",
            "memory": "migrated",
            "user_id": "u1",
            "metadata": {},
        }

        outcome = MemoryApplicationService(
            repository,
            mode="shadow",
        ).backfill_legacy(mem, "u1")

        assert outcome["coverage"]["provider_count"] == 1
        reset_repository_cache_for_tests()
        canonical_repository = CanonicalRepository(repository.path)
        service = MemoryApplicationService(canonical_repository, mode="canonical")
        created = service.create_fact(
            mem,
            user_id="u1",
            text="after cutover",
            metadata={},
        )
        assert created["canonical_id"]

    def test_shadow_backfill_fails_closed_on_unproven_requested_namespace(
        self,
        repository,
        projection_paths,
    ):
        class UntrustedMem0(FakeMem0):
            def get_all(self, *, user_id, filters=None, limit=None):
                rows = list(self.rows.values())
                return {"results": rows[:limit] if limit is not None else rows}

        mem = UntrustedMem0()
        mem.rows = {
            "valid": {
                "id": "valid",
                "memory": "owned",
                "user_id": "u1",
                "metadata": {},
            },
            "wrong": {
                "id": "wrong",
                "memory": "foreign",
                "user_id": "u2",
                "metadata": {},
            },
            "missing": {
                "id": "missing",
                "memory": "unscoped",
                "metadata": {},
            },
        }

        with pytest.raises(RuntimeError, match="does not prove requested namespace"):
            MemoryApplicationService(
                repository,
                mode="shadow",
            ).backfill_legacy(mem, "u1")

        assert repository.migration_users() == []
        assert repository.get_fact("valid") is None
        assert repository.get_fact("wrong") is None
        assert repository.get_fact("missing") is None

    def test_canonical_coverage_rejects_missing_provider_identity(
        self,
        repository,
    ):
        mem = FakeMem0()
        del mem.storage_identity
        service = MemoryApplicationService(repository, mode="canonical")

        with pytest.raises(RuntimeError, match="storage identity is unavailable"):
            service.create_fact(mem, user_id="u1", text="blocked", metadata={})

        assert mem.get_all_calls == 0
        assert repository.list_facts("u1", include_inactive=True) == []

    def test_canonical_coverage_cache_is_scoped_to_provider_storage(
        self,
        repository,
    ):
        service = MemoryApplicationService(repository, mode="canonical")
        first = FakeMem0(collection="collection-a")
        replacement = FakeMem0(backend="replacement", collection="collection-a")
        other_collection = FakeMem0(collection="collection-b")

        service._ensure_canonical_ready(first, "u1")
        service._ensure_canonical_ready(first, "u1")
        service._ensure_canonical_ready(replacement, "u1")
        service._ensure_canonical_ready(other_collection, "u1")

        assert first.get_all_calls == 1
        assert replacement.get_all_calls == 1
        assert other_collection.get_all_calls == 1
        with repository._read() as conn:
            rows = conn.execute(
                "SELECT provider_backend,provider_collection FROM migration_coverage",
            ).fetchall()
        assert {
            (row["provider_backend"], row["provider_collection"]) for row in rows
        } == {
            ("fake", "collection-a"),
            ("replacement", "collection-a"),
            ("fake", "collection-b"),
        }

    def test_replay_idempotency_key_is_stable_at_canonical_boundary(
        self,
        repository,
        projection_paths,
    ):
        mem = FakeMem0()
        service = MemoryApplicationService(repository, mode="shadow")
        metadata = {"source": "replay", "idempotency_key": "work-key"}

        first = service.create_fact(
            mem,
            user_id="u1",
            text="same replay fact",
            metadata=metadata,
        )
        second = service.create_fact(
            mem,
            user_id="u1",
            text="same replay fact",
            metadata=metadata,
        )

        assert first["canonical_id"] == second["canonical_id"]
        assert mem.add_calls == 1
        assert len(repository.list_facts("u1", include_inactive=True)) == 1

    def test_batch_idempotency_key_is_stable_at_canonical_boundary(
        self,
        repository,
        projection_paths,
    ):
        mem = FakeMem0()
        service = MemoryApplicationService(repository, mode="shadow")
        metadata = {"source": "batch", "idempotency_key": "batch-work-key"}

        first = service.create_fact(
            mem,
            user_id="u1",
            text="same batch fact",
            metadata=metadata,
        )
        second = service.create_fact(
            mem,
            user_id="u1",
            text="same batch fact",
            metadata=metadata,
        )

        assert first["canonical_id"] == second["canonical_id"]
        assert mem.add_calls == 1
        assert len(repository.list_facts("u1", include_inactive=True)) == 1

    def test_legacy_inference_results_are_adopted_before_projection(
        self,
        repository,
        projection_paths,
    ):
        class InferenceMem0(FakeMem0):
            def add(self, messages, *, user_id, metadata):
                self.add_calls += 1
                self.rows["inferred-1"] = {
                    "id": "inferred-1",
                    "memory": "inferred fact",
                    "user_id": str(user_id),
                    "metadata": dict(metadata),
                }
                return {"results": [{"id": "inferred-1"}]}

        mem = InferenceMem0()
        service = MemoryApplicationService(repository, mode="shadow")

        result = service.ingest_legacy_messages(
            mem,
            [{"role": "user", "content": "raw conversation"}],
            user_id="u1",
            metadata={"source": "conversation"},
        )

        fact = repository.get_fact("inferred-1")
        assert fact is not None
        assert fact["text"] == "inferred fact"
        assert result["canonical_ids"] == ["inferred-1"]
        assert result["_projections_managed"] is True
        assert [
            row["projection"]
            for row in repository.outbox_rows(
                aggregate_id="inferred-1",
            )
        ] == ["bm25", "graph", "fact_store"]
        assert all(
            row["status"] == "acked"
            for row in repository.outbox_rows(aggregate_id="inferred-1")
        )

    def test_create_reinforce_archive_delete_use_one_boundary(
        self,
        repository,
        projection_paths,
    ):
        mem = FakeMem0()
        service = MemoryApplicationService(repository, mode="canonical")
        result = service.create_fact(
            mem,
            user_id="u1",
            text="Pythonが好き",
            metadata={"attribute": "preference.language"},
            entities=[{"name": "Python", "type": "tech"}],
        )
        canonical_id = result["canonical_id"]
        legacy_id = repository.get_fact(canonical_id)["legacy_id"]

        assert service.reinforce(mem, legacy_id, channel="mention")
        assert repository.get_fact(canonical_id)["metadata"]["mention_count"] == 2
        assert service.archive(mem, legacy_id, reason="update")
        assert repository.get_fact(canonical_id)["status"] == "archived"
        assert service.delete(mem, legacy_id)
        assert repository.get_fact(canonical_id)["status"] == "deleted"
        assert legacy_id not in mem.rows

    def test_merge_routes_through_atomic_consolidation(
        self,
        repository,
        projection_paths,
    ):
        mem = FakeMem0()
        service = MemoryApplicationService(repository, mode="canonical")
        first = service.create_fact(
            mem,
            user_id="u1",
            text="Rustを使う",
            metadata={},
        )["canonical_id"]
        second = service.create_fact(
            mem,
            user_id="u1",
            text="Rust asyncを使う",
            metadata={},
        )["canonical_id"]

        merged = service.merge(
            mem,
            user_id="u1",
            source_ids=[first, second],
            text="Rustのasync実装を使う",
            metadata={"source": "merge"},
        )

        assert merged["status"] == "active"
        assert repository.get_fact(first)["status"] == "archived"
        assert repository.get_fact(second)["merged_into"] == merged["fact_id"]

    def test_cross_user_consolidation_rolls_back(self, repository):
        repository.create_fact("u1", "private", {}, fact_id="u1-fact")

        with pytest.raises(ValueError, match="another user"):
            repository.consolidate(
                "u2",
                ["u1-fact"],
                [{"fact_id": "invalid", "text": "must rollback"}],
            )

        assert repository.get_fact("invalid") is None
        assert repository.get_fact("u1-fact")["status"] == "active"

    def test_relations_are_canonical_and_graph_is_a_projection(
        self,
        repository,
        projection_paths,
    ):
        _, graph, _ = projection_paths
        mem = FakeMem0()
        service = MemoryApplicationService(repository, mode="canonical")
        service.create_fact(
            mem,
            user_id="u1",
            text="Alice fact",
            metadata={},
            entities=[{"name": "Alice", "type": "person"}],
        )
        service.create_fact(
            mem,
            user_id="u1",
            text="Bob fact",
            metadata={},
            entities=[{"name": "Bob", "type": "person"}],
        )

        added = service.record_relations(
            mem,
            "u1",
            [{"src": "Alice", "rel_type": "knows", "dst": "Bob"}],
        )

        assert added == 1
        assert repository.list_relations("u1")[0]["rel_type"] == "knows"
        alice = graph.find_entity("Alice", "u1", fuzzy=False)
        assert alice is not None
        assert graph.get_relations(alice.id, direction="src")[0].rel_type == "knows"
        relation_events = [
            row
            for row in repository.outbox_rows()
            if row["operation"] == "relation_upsert"
        ]
        assert len(relation_events) == 1
        assert relation_events[0]["status"] == "acked"
        assert (
            service.record_relations(
                mem,
                "u1",
                [{"src": "Alice", "rel_type": "knows", "dst": "Bob"}],
            )
            == 0
        )

    @pytest.mark.parametrize("mode", ["shadow", "canonical"])
    def test_managed_modes_require_projection_capability_for_all_mutations(
        self,
        monkeypatch,
        projection_paths,
        mode,
    ):
        bm25, graph, facts = projection_paths
        monkeypatch.setenv("MEM0_INTEGRITY_MODE", mode)

        with pytest.raises(RuntimeError, match="direct BM25 mutation"):
            bm25.index_fact("f1", "text", "u1")
        with pytest.raises(RuntimeError, match="internal capability"):
            bm25.apply_projection("e1", "upsert", "f1", "text", "u1")
        with pytest.raises(RuntimeError, match="internal capability"):
            bm25.reindex_all([("f1", "text", "u1")])
        with pytest.raises(RuntimeError, match="direct graph mutation"):
            graph.upsert_entity("Alice", "person", "u1")
        with pytest.raises(RuntimeError, match="internal capability"):
            graph.apply_fact_projection(
                "e1",
                "f1",
                "u1",
                [{"name": "Alice", "type": "person"}],
            )
        with pytest.raises(RuntimeError, match="internal capability"):
            graph.apply_relation_projection("e2", "Alice", "knows", "Bob", "u1")
        with pytest.raises(RuntimeError, match="direct fact-store mutation"):
            facts.record_fact("f1", "u1", "profile.name", value_text="Alice")
        with pytest.raises(RuntimeError, match="internal capability"):
            facts.apply_projection(
                "e1",
                "upsert",
                "f1",
                "u1",
                "profile.name",
                value_text="Alice",
            )

    def test_shadow_bm25_search_hides_orphan_projection(
        self,
        monkeypatch,
        repository,
        projection_paths,
    ):
        from services.mem0 import integrity

        bm25, _, _ = projection_paths
        monkeypatch.setenv("MEM0_INTEGRITY_MODE", "legacy")
        assert bm25.index_fact("orphan", "Rust orphan", "u1")
        assert bm25.search_bm25("Rust", "u1")

        monkeypatch.setattr(integrity, "get_repository", lambda path=None: repository)
        monkeypatch.setenv("MEM0_INTEGRITY_MODE", "shadow")
        assert bm25.search_bm25("Rust", "u1") == []


@pytest.mark.parametrize("queue_name", ["batch", "replay"])
def test_work_queue_lease_expiry_retry_dead_letter_and_cursor(repository, queue_name):
    repository.enqueue_work(
        queue_name,
        "partition",
        "cursor-1",
        {"value": 1},
        max_attempts=2,
    )
    first = repository.lease_work(
        queue_name,
        "worker-1",
        partition_key="partition",
        lease_seconds=0.01,
    )[0]
    time.sleep(0.02)
    second = repository.lease_work(
        queue_name,
        "worker-2",
        partition_key="partition",
    )[0]
    assert second.id == first.id
    assert second.attempts == 2

    assert repository.retry_work(second, "injected", retry_seconds=0) == "dead"
    assert repository.work_cursor(queue_name, "partition") == "cursor-1"
    assert repository.work_rows(queue_name)[0]["status"] == "dead"
    assert repository.dead_letters("work")


def test_work_queue_blocks_later_cursor_until_first_is_acked(repository):
    repository.enqueue_work("batch", "p", "1", {"value": 1})
    repository.enqueue_work("batch", "p", "2", {"value": 2})

    first = repository.lease_work("batch", "w", partition_key="p")[0]
    assert first.cursor_value == "1"
    assert repository.lease_work("batch", "other", partition_key="p") == []
    assert repository.retry_work(first, "retry", retry_seconds=0) == "pending"
    first_retry = repository.lease_work("batch", "w", partition_key="p")[0]
    assert repository.ack_work(first_retry)
    second = repository.lease_work("batch", "w", partition_key="p")[0]
    assert second.cursor_value == "2"
    assert repository.ack_work(second)
    assert repository.work_cursor("batch", "p") == "2"


def test_expired_work_lease_is_fenced_even_with_same_owner(repository):
    repository.enqueue_work("batch", "p", "1", {})
    first = repository.lease_work(
        "batch",
        "same",
        partition_key="p",
        lease_seconds=0.01,
    )[0]
    time.sleep(0.02)
    second = repository.lease_work("batch", "same", partition_key="p")[0]

    assert first.token != second.token
    assert not repository.ack_work(first)
    assert repository.retry_work(first, "stale") == "lost"
    assert repository.ack_work(second)


def test_expired_work_lease_cannot_ack_retry_or_advance_cursor(repository):
    repository.enqueue_work("replay", "p", "cursor-1", {})
    lease = repository.lease_work(
        "replay",
        "worker",
        partition_key="p",
        lease_seconds=0.01,
    )[0]
    time.sleep(0.02)

    assert not repository.ack_work(lease)
    assert repository.retry_work(lease, "expired") == "lost"
    assert repository.work_cursor("replay", "p") == ""
    assert repository.work_rows("replay")[0]["status"] == "leased"


def test_dead_letter_redrive_is_generation_tagged_and_resolved(repository):
    repository.enqueue_work(
        "batch",
        "p",
        "1",
        {"value": 1},
        max_attempts=1,
    )
    lease = repository.lease_work("batch", "worker", partition_key="p")[0]
    assert repository.retry_work(lease, "injected") == "dead"
    dead = repository.dead_letters("work", status="open")[0]

    redrive_id = repository.redrive_dead_letter(dead["id"])
    redrive = repository.lease_work("batch", "worker", partition_key="p")[0]

    assert redrive.id == redrive_id
    assert redrive.generation == 1
    assert repository.ack_work(redrive)
    resolved = repository.dead_letters("work")[0]
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None


def test_failed_redrive_opens_next_generation(repository):
    repository.enqueue_work("batch", "p", "1", {}, max_attempts=1)
    first = repository.lease_work("batch", "worker", partition_key="p")[0]
    repository.retry_work(first, "generation zero")
    original = repository.dead_letters("work", status="open")[0]
    repository.redrive_dead_letter(original["id"])
    redrive = repository.lease_work("batch", "worker", partition_key="p")[0]

    assert repository.retry_work(redrive, "generation one") == "dead"

    assert repository.dead_letters("work")[0]["status"] == "resolved"
    open_items = repository.dead_letters("work", status="open")
    assert len(open_items) == 1
    assert open_items[0]["generation"] == 1


def test_projection_consumer_drains_startup_pending_and_stops(
    repository,
    projection_paths,
):
    mem = FakeMem0()
    repository.create_fact("u1", "pending", {}, fact_id="startup")
    consumer = ensure_projection_consumer(
        lambda: mem,
        repository=repository,
        interval_seconds=0.01,
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(row["status"] == "acked" for row in repository.outbox_rows()):
            break
        time.sleep(0.01)
    shutdown_projection_consumers(timeout=1)

    assert all(row["status"] == "acked" for row in repository.outbox_rows())
    assert not consumer.alive


def test_projection_consumer_drains_pending_work_during_shutdown(
    repository,
    projection_paths,
):
    shutdown_projection_consumers(timeout=1)
    mem = FakeMem0()
    factory_entered = threading.Event()
    allow_factory = threading.Event()

    def mem_factory():
        factory_entered.set()
        assert allow_factory.wait(1)
        return mem

    consumer = ensure_projection_consumer(
        mem_factory,
        repository=repository,
        interval_seconds=10,
    )
    assert factory_entered.wait(1)
    repository.create_fact("u1", "pending", {}, fact_id="shutdown-drain")
    allow_factory.set()

    shutdown_projection_consumers(timeout=2)

    assert all(row["status"] == "acked" for row in repository.outbox_rows())
    assert not consumer.alive


def test_projection_consumer_join_timeout_preserves_registry_owner(
    repository,
    projection_paths,
):
    shutdown_projection_consumers(timeout=1)

    class BlockingMem0(FakeMem0):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def add(self, messages, *, user_id, metadata, infer):
            self.entered.set()
            assert self.release.wait(2)
            return super().add(
                messages,
                user_id=user_id,
                metadata=metadata,
                infer=infer,
            )

    mem = BlockingMem0()
    repository.create_fact("u1", "blocked", {}, fact_id="single-owner")
    consumer = ensure_projection_consumer(
        lambda: mem,
        repository=repository,
        interval_seconds=0.01,
    )
    try:
        assert mem.entered.wait(1)
        shutdown_projection_consumers(timeout=0.01)
        assert consumer.alive

        same_consumer = ensure_projection_consumer(
            lambda: FakeMem0(),
            repository=repository,
            interval_seconds=0.01,
        )
        assert same_consumer is consumer
    finally:
        mem.release.set()
        shutdown_projection_consumers(timeout=2)

    assert not consumer.alive


def test_projection_consumer_alerts_open_dead_letter(repository):
    repository.enqueue_work("batch", "p", "1", {}, max_attempts=1)
    lease = repository.lease_work("batch", "worker", partition_key="p")[0]
    repository.retry_work(lease, "alert me")
    notified = []
    event = threading.Event()

    def notify(item):
        notified.append(item)
        event.set()

    consumer = ensure_projection_consumer(
        lambda: None,
        repository=repository,
        interval_seconds=0.01,
        notifier=notify,
    )
    assert event.wait(1)
    shutdown_projection_consumers(timeout=1)

    assert notified[0]["last_error"] == "alert me"
    assert not consumer.alive


def test_batch_public_lease_ack_and_retry(tmp_path):
    from services.mem0 import batch

    batch.append_turn(tmp_path, 10, "user", "assistant")
    leased = batch.lease_batch(tmp_path, 10, worker_id="worker")
    assert leased.turns[0]["user"] == "user"
    assert (
        batch.retry_batch(tmp_path, leased, "transient", retry_seconds=0) == "pending"
    )

    retried = batch.lease_batch(tmp_path, 10, worker_id="worker")
    assert retried.lease.id == leased.lease.id
    assert batch.ack_batch(tmp_path, retried)
    repository = get_repository(tmp_path / "mem0_canonical.sqlite")
    assert repository.work_cursor("batch", "10") == retried.lease.cursor_value


def test_batch_finalize_failure_preserves_sqlite_accumulator(tmp_path):
    from services.mem0 import batch

    batch.append_turn(tmp_path, 10, "user", "assistant")
    repository = get_repository(tmp_path / "mem0_canonical.sqlite")
    with repository._tx() as conn:
        conn.execute(
            """CREATE TRIGGER fail_batch_finalize BEFORE INSERT ON work_items
               BEGIN SELECT RAISE(ABORT, 'injected finalize failure'); END""",
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected finalize failure"):
        batch.lease_batch(tmp_path, 10, worker_id="worker")
    with repository._tx() as conn:
        conn.execute("DROP TRIGGER fail_batch_finalize")

    recovered = batch.lease_batch(tmp_path, 10, worker_id="worker")
    assert recovered is not None
    assert [turn["user"] for turn in recovered.turns] == ["user"]
    assert batch.ack_batch(tmp_path, recovered)


def test_batch_parallel_appends_do_not_lose_turns(tmp_path):
    from services.mem0 import batch

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(
            pool.map(
                lambda index: batch.append_turn(
                    tmp_path,
                    99,
                    f"user-{index}",
                    f"assistant-{index}",
                ),
                range(40),
            )
        )

    leased = batch.lease_batch(tmp_path, 99, worker_id="worker")
    assert leased is not None
    assert len(leased.turns) == 40
    assert max(counts) == 40
    assert len({turn["user"] for turn in leased.turns}) == 40
    assert batch.ack_batch(tmp_path, leased)


def test_pop_batch_no_longer_acknowledges_before_processing(tmp_path):
    from services.mem0 import batch

    batch.append_turn(tmp_path, 10, "user", "assistant")
    leased = batch.pop_batch(tmp_path, 10)
    assert leased is not None
    turns, facts = leased
    assert turns[0]["user"] == "user"
    assert facts == []
    repository = get_repository(tmp_path / "mem0_canonical.sqlite")
    assert repository.work_rows("batch")[0]["status"] == "leased"
    assert repository.work_cursor("batch", "10") == ""
    assert batch.finish_compat_batch(tmp_path, 10) is True
    assert repository.work_rows("batch")[0]["status"] == "acked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"batch_size": ""},
        {"batch_size": "1"},
        {"batch_size": "invalid", "channel_id": "10"},
        {"batch_size": "1", "channel_id": ""},
        {"batch_size": "1", "channel_id": "10", "source": "replay"},
        {"batch_size": "1", "channel_id": "10", "source": "batch"},
        {"idempotency_key": "unowned"},
    ],
)
async def test_batch_metadata_without_owned_work_fails_before_provider(
    monkeypatch,
    metadata,
):
    from services.mem0 import ingest

    provider = AsyncMock(return_value={"results": [], "kept": 0})
    monkeypatch.setattr(ingest, "_add_memories_impl", provider)

    result = await ingest.add_memories(
        user_text="must not ingest",
        user_id="u1",
        metadata=metadata,
    )

    assert result["error_code"] == "batch_lease_unavailable"
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_inherited_batch_work_rejects_ambiguous_metadata_before_provider(
    monkeypatch,
    tmp_path,
):
    import config
    from services.mem0 import batch, ingest

    reset_repository_cache_for_tests()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    batch.append_turn(tmp_path, 10, "user", "assistant")
    claimed = await asyncio.to_thread(
        batch.lease_batch,
        tmp_path,
        10,
        worker_id="ambiguous-owner",
        lease_seconds=0.1,
    )
    assert claimed is not None
    list(claimed)
    provider = AsyncMock(return_value={"results": [], "kept": 0})
    monkeypatch.setattr(ingest, "_add_memories_impl", provider)

    result = await ingest.add_memories(
        user_text="must not ingest",
        user_id="u1",
        metadata={"channel_id": "10"},
    )

    assert result["error_code"] == "batch_lease_unavailable"
    provider.assert_not_awaited()
    assert (
        await asyncio.to_thread(
            batch.retry_batch,
            tmp_path,
            claimed,
            "test cleanup",
            retry_seconds=0,
        )
        == "pending"
    )


@pytest.mark.asyncio
async def test_expired_batch_identity_requires_reacquire_before_provider(
    monkeypatch,
    tmp_path,
):
    import config
    from services.mem0 import batch, ingest

    reset_repository_cache_for_tests()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    batch.append_turn(tmp_path, 10, "user", "assistant")
    expired = await asyncio.to_thread(
        batch.lease_batch,
        tmp_path,
        10,
        worker_id="expired-worker",
        lease_seconds=0.05,
    )
    assert expired is not None
    list(expired)
    repository = get_repository(tmp_path / "mem0_canonical.sqlite")
    entered = threading.Event()
    release = threading.Event()
    provider_calls = 0
    side_effects = 0
    seen_metadata = []

    async def blocking_ingest(*_args, metadata=None, **_kwargs):
        nonlocal provider_calls, side_effects
        provider_calls += 1
        supplied_metadata = metadata if metadata is not None else _args[2]
        seen_metadata.append(dict(supplied_metadata or {}))

        def provider_side_effect():
            nonlocal side_effects
            entered.set()
            assert release.wait(timeout=5)
            side_effects += 1
            return {"results": [{"id": "provider-1"}], "kept": 1}

        return await asyncio.to_thread(provider_side_effect)

    monkeypatch.setattr(ingest, "_add_memories_impl", blocking_ingest)
    await asyncio.sleep(0.09)

    rejected = await ingest.add_memories(
        user_text="summary",
        user_id="u1",
        metadata={"batch_size": "1", "channel_id": "10"},
    )

    assert rejected["error_code"] == "batch_lease_unavailable"
    assert provider_calls == 0
    assert side_effects == 0
    assert repository.work_cursor("batch", "10") == ""

    successor = await asyncio.to_thread(
        batch.lease_batch,
        tmp_path,
        10,
        worker_id="successor-worker",
        lease_seconds=0.5,
    )
    assert successor is not None
    assert successor.lease.id == expired.lease.id
    assert successor.lease.token != expired.lease.token
    list(successor)

    task = asyncio.create_task(
        ingest.add_memories(
            user_text="summary",
            user_id="u1",
            metadata={"batch_size": "1", "channel_id": "10"},
        ),
    )
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.75)
    competing = await asyncio.to_thread(
        batch.lease_batch,
        tmp_path,
        10,
        worker_id="competing-worker",
        lease_seconds=0.5,
    )
    assert competing is None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider_calls == 1
    assert side_effects == 1
    assert seen_metadata == [
        {
            "batch_size": "1",
            "channel_id": "10",
            "source": "batch",
            "idempotency_key": successor.lease.key,
        }
    ]
    row = repository.work_rows("batch")[0]
    assert row["status"] == "acked"
    assert repository.work_cursor("batch", "10") == successor.lease.cursor_value


@pytest.mark.asyncio
async def test_batch_finish_clears_claim_in_async_caller_context(
    monkeypatch,
    tmp_path,
):
    import config
    from services.mem0 import batch, ingest

    reset_repository_cache_for_tests()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    batch.append_turn(tmp_path, 10, "user", "assistant")
    claimed = batch.lease_batch(tmp_path, 10, worker_id="worker")
    assert claimed is not None
    list(claimed)
    operation = batch.begin_compat_batch(tmp_path, 10)
    assert operation is not None
    assert await operation.finish() is True
    assert batch.current_compat_batch_claims() == ()

    provider = AsyncMock(return_value={"results": [], "kept": 0})
    monkeypatch.setattr(ingest, "_add_memories_impl", provider)
    result = await ingest.add_memories(
        user_text="ordinary follow-up",
        user_id="u1",
        metadata={},
    )

    assert result == {"results": [], "kept": 0}
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_repeated_cancel_drains_once_and_heartbeats_short_lease(
    monkeypatch,
    tmp_path,
):
    import config
    from services.mem0 import batch, ingest

    reset_repository_cache_for_tests()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    batch.append_turn(tmp_path, 10, "user", "assistant")
    claimed = await asyncio.to_thread(
        batch.lease_batch,
        tmp_path,
        10,
        worker_id="first-worker",
        lease_seconds=0.5,
    )
    assert claimed is not None
    list(claimed)
    repository = get_repository(tmp_path / "mem0_canonical.sqlite")
    entered = threading.Event()
    release = threading.Event()
    side_effects = 0
    seen_metadata = []

    async def blocking_ingest(*_args, metadata=None, **_kwargs):
        nonlocal side_effects
        supplied_metadata = metadata if metadata is not None else _args[2]
        seen_metadata.append(dict(supplied_metadata or {}))

        def provider_side_effect():
            nonlocal side_effects
            entered.set()
            assert release.wait(timeout=5)
            side_effects += 1
            return {"results": [{"id": "provider-1"}], "kept": 1}

        return await asyncio.to_thread(provider_side_effect)

    monkeypatch.setattr(ingest, "_add_memories_impl", blocking_ingest)
    task = asyncio.create_task(
        ingest.add_memories(
            user_text="summary",
            user_id="u1",
            metadata={"batch_size": "1", "channel_id": "10"},
        ),
    )
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.75)

    competing = await asyncio.to_thread(
        batch.lease_batch,
        tmp_path,
        10,
        worker_id="second-worker",
        lease_seconds=0.5,
    )
    assert competing is None
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert side_effects == 1
    assert seen_metadata == [
        {
            "batch_size": "1",
            "channel_id": "10",
            "source": "batch",
            "idempotency_key": claimed.lease.key,
        }
    ]
    row = repository.work_rows("batch")[0]
    assert row["status"] == "acked"
    assert repository.work_cursor("batch", "10") == claimed.lease.cursor_value
    assert not any(
        pending.get_name().startswith(
            (
                "mem0-batch-heartbeat-",
                "mem0-batch-provider-",
            )
        )
        for pending in asyncio.all_tasks()
        if pending is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_compat_predecessor_cannot_ack_reclaimed_successor(tmp_path):
    from services.mem0 import batch

    batch.append_turn(tmp_path, 10, "user", "assistant")
    repository = get_repository(tmp_path / "mem0_canonical.sqlite")
    predecessor_ready = asyncio.Event()
    successor_ready = asyncio.Event()
    finish_predecessor = asyncio.Event()
    finish_successor = asyncio.Event()
    identities = {}
    outcomes = {}

    async def predecessor():
        claimed = await asyncio.to_thread(batch.pop_batch, tmp_path, 10)
        assert claimed is not None
        list(claimed)
        identities["predecessor"] = batch.get_compat_batch_identity(tmp_path, 10)
        predecessor_ready.set()
        await finish_predecessor.wait()
        outcomes["predecessor"] = batch.finish_compat_batch(tmp_path, 10)

    async def successor():
        claimed = await asyncio.to_thread(batch.pop_batch, tmp_path, 10)
        assert claimed is not None
        list(claimed)
        identities["successor"] = batch.get_compat_batch_identity(tmp_path, 10)
        successor_ready.set()
        await finish_successor.wait()
        outcomes["successor"] = batch.finish_compat_batch(tmp_path, 10)

    predecessor_task = asyncio.create_task(predecessor())
    await predecessor_ready.wait()
    with repository._tx() as conn:
        conn.execute(
            "UPDATE work_items SET lease_expires_at=? WHERE queue_name='batch'",
            (time.time() - 1,),
        )
    successor_task = asyncio.create_task(successor())
    await successor_ready.wait()
    assert identities["predecessor"][1] != identities["successor"][1]

    finish_predecessor.set()
    await predecessor_task
    row = repository.work_rows("batch")[0]
    assert outcomes["predecessor"] is False
    assert row["status"] == "leased"
    assert row["lease_token"] == identities["successor"][1]

    finish_successor.set()
    await successor_task
    assert outcomes["successor"] is True
    assert repository.work_rows("batch")[0]["status"] == "acked"
    assert batch._compat_leases == {}


@pytest.mark.asyncio
async def test_compat_claim_transfers_only_to_finishing_child_task(tmp_path):
    from services.mem0 import batch

    batch.append_turn(tmp_path, 10, "user", "assistant")
    claimed = await asyncio.to_thread(batch.pop_batch, tmp_path, 10)
    assert claimed is not None
    list(claimed)
    parent_identity = batch.get_compat_batch_identity(tmp_path, 10)

    async def finish_in_child():
        child_identity = batch.get_compat_batch_identity(tmp_path, 10)
        return child_identity, batch.finish_compat_batch(tmp_path, 10)

    child_identity, result = await asyncio.create_task(finish_in_child())

    assert child_identity[0] != parent_identity[0]
    assert child_identity[1] == parent_identity[1]
    assert result is True
    assert (
        batch.finish_compat_batch(
            tmp_path,
            10,
            owner_task=parent_identity[0],
            lease_token=parent_identity[1],
        )
        is False
    )


@pytest.mark.asyncio
async def test_compat_old_owner_cannot_reregister_after_child_transfer(tmp_path):
    from services.mem0 import batch

    batch.append_turn(tmp_path, 10, "user", "assistant")
    claimed = await asyncio.to_thread(batch.pop_batch, tmp_path, 10)
    assert claimed is not None
    list(claimed)
    parent_identity = batch.get_compat_batch_identity(tmp_path, 10)
    child_ready = asyncio.Event()
    finish_child = asyncio.Event()

    async def child():
        identity = batch.get_compat_batch_identity(tmp_path, 10)
        child_ready.set()
        await finish_child.wait()
        return identity, batch.finish_compat_batch(tmp_path, 10)

    child_task = asyncio.create_task(child())
    await child_ready.wait()

    # A second tuple-unpack of the same object by the predecessor used to
    # recreate its registry entry and let it ACK before the ingest child.
    list(claimed)
    assert (
        batch.finish_compat_batch(
            tmp_path,
            10,
            owner_task=parent_identity[0],
            lease_token=parent_identity[1],
        )
        is False
    )
    repository = get_repository(tmp_path / "mem0_canonical.sqlite")
    assert repository.work_rows("batch")[0]["status"] == "leased"

    finish_child.set()
    child_identity, result = await child_task
    assert child_identity[0] != parent_identity[0]
    assert result is True
    assert repository.work_rows("batch")[0]["status"] == "acked"
    assert batch._compat_leases == {}


@pytest.mark.asyncio
async def test_compat_registry_cleans_task_exit_and_lease_expiry(tmp_path):
    from services.mem0 import batch

    batch.append_turn(tmp_path, 10, "first", "assistant")

    async def abandon_owner():
        claimed = await asyncio.to_thread(
            batch.lease_batch,
            tmp_path,
            10,
            worker_id="abandoned",
            lease_seconds=0.05,
        )
        assert claimed is not None
        list(claimed)

    await asyncio.create_task(abandon_owner())
    await asyncio.sleep(0)
    assert batch._compat_leases == {}

    repository = get_repository(tmp_path / "mem0_canonical.sqlite")
    with repository._tx() as conn:
        conn.execute(
            "UPDATE work_items SET lease_expires_at=? WHERE queue_name='batch'",
            (time.time() - 1,),
        )
    successor = await asyncio.to_thread(
        batch.lease_batch,
        tmp_path,
        10,
        worker_id="successor",
        lease_seconds=0.02,
    )
    assert successor is not None
    list(successor)
    assert batch._compat_leases
    await asyncio.sleep(0.05)
    assert batch._compat_leases == {}
    assert batch.finish_compat_batch(tmp_path, 10) is False


@pytest.mark.asyncio
async def test_replay_failure_does_not_advance_cursor_then_retries(
    monkeypatch,
    tmp_state_dir,
):
    from services.mem0 import replay

    reset_repository_cache_for_tests()
    lines = [
        {"ts": "t1", "role": "user", "author": "", "text": "Rustを使う"},
        {"ts": "t2", "role": "assistant", "author": "", "text": "記録する"},
    ]
    monkeypatch.setattr(
        "services.mem0.history_index.get_lines_since",
        lambda _channel, _since, _limit: lines,
    )
    add = AsyncMock(side_effect=[RuntimeError("injected"), {"kept": 1}])
    monkeypatch.setattr("services.mem0.ingest.add_memories", add)

    original_retry = CanonicalRepository.retry_work

    def immediate_retry(self, lease, error, *, retry_seconds=0):
        return original_retry(self, lease, error, retry_seconds=0)

    monkeypatch.setattr(CanonicalRepository, "retry_work", immediate_retry)

    failed = await replay.replay_channel_history(123, "u1", dry_run=False)
    assert failed["cursor_advanced_to"] == ""
    assert replay._load_cursor() == {}

    recovered = await replay.replay_channel_history(123, "u1", dry_run=False)
    assert recovered["facts_kept"] == 1
    assert recovered["cursor_advanced_to"] == "t2"
    assert replay._load_cursor()["123"] == "t2"


@pytest.mark.asyncio
async def test_replay_acknowledges_trailing_non_turn_rows(
    monkeypatch,
    tmp_state_dir,
):
    from services.mem0 import replay

    reset_repository_cache_for_tests()
    lines = [
        {"ts": "t1", "role": "channel", "author": "other", "text": "ambient"},
        {"ts": "t2", "role": "channel", "author": "other", "text": "ambient2"},
    ]
    monkeypatch.setattr(
        "services.mem0.history_index.get_lines_since",
        lambda _channel, _since, _limit: lines,
    )
    add = AsyncMock()
    monkeypatch.setattr("services.mem0.ingest.add_memories", add)

    result = await replay.replay_channel_history(123, "u1", dry_run=False)

    assert result["turns_processed"] == 0
    assert result["cursor_advanced_to"] == "t2"
    assert replay._load_cursor()["123"] == "t2"
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_lost_lease_does_not_advance_json_cursor(
    monkeypatch,
    tmp_state_dir,
):
    from services.mem0 import replay

    reset_repository_cache_for_tests()
    lines = [
        {"ts": "t1", "role": "user", "author": "", "text": "Rust"},
        {"ts": "t2", "role": "assistant", "author": "", "text": "record"},
    ]
    monkeypatch.setattr(
        "services.mem0.history_index.get_lines_since",
        lambda _channel, _since, _limit: lines,
    )
    monkeypatch.setattr(
        "services.mem0.ingest.add_memories",
        AsyncMock(return_value={"kept": 1}),
    )
    monkeypatch.setattr(CanonicalRepository, "ack_work", lambda self, lease: False)

    result = await replay.replay_channel_history(123, "u1", dry_run=False)

    assert result["cursor_advanced_to"] == ""
    assert replay._load_cursor() == {}


@pytest.mark.asyncio
async def test_replay_heartbeat_prevents_reclaim_during_provider_call(
    monkeypatch,
    tmp_state_dir,
):
    from services.mem0 import integrity, replay

    reset_repository_cache_for_tests()
    repository = get_repository()
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowService:
        async def replay(self, _payload, _ingest):
            entered.set()
            await release.wait()
            return {"kept": 1}

    monkeypatch.setattr(replay, "REPLAY_WORK_LEASE_SECONDS", 0.03)
    monkeypatch.setattr(replay, "REPLAY_WORK_HEARTBEAT_INTERVAL", 0.005)
    monkeypatch.setattr(integrity, "get_application_service", lambda: SlowService())
    monkeypatch.setattr(
        "services.mem0.history_index.get_lines_since",
        lambda _channel, _since, _limit: [
            {"ts": "t1", "role": "user", "author": "", "text": "Rust"},
            {"ts": "t2", "role": "assistant", "author": "", "text": "record"},
        ],
    )

    task = asyncio.create_task(
        replay.replay_channel_history(123, "u1", dry_run=False),
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0.08)
        competing = await asyncio.to_thread(
            repository.lease_work,
            "replay",
            "competing-worker",
            partition_key="123",
        )
        assert competing == []
    finally:
        release.set()
    result = await task

    assert result["cursor_advanced_to"] == "t2"


@pytest.mark.asyncio
async def test_replay_fence_loss_skips_provider_side_effect(
    monkeypatch,
    tmp_state_dir,
):
    from services.mem0 import integrity, replay

    reset_repository_cache_for_tests()
    repository = get_repository()
    provider_call = AsyncMock(return_value={"kept": 1})
    service = type("Service", (), {"replay": provider_call})()
    monkeypatch.setattr(replay, "REPLAY_WORK_LEASE_SECONDS", 1.0)
    monkeypatch.setattr(replay, "REPLAY_WORK_HEARTBEAT_INTERVAL", 10.0)
    monkeypatch.setattr(integrity, "get_application_service", lambda: service)
    monkeypatch.setattr(
        "services.mem0.history_index.get_lines_since",
        lambda _channel, _since, _limit: [
            {"ts": "t1", "role": "user", "author": "", "text": "Rust"},
            {"ts": "t2", "role": "assistant", "author": "", "text": "record"},
        ],
    )
    original_renew = repository.renew_work
    renewals = 0
    successor = []

    def lose_on_fence(lease, *, lease_seconds):
        nonlocal renewals
        renewals += 1
        if renewals == 1:
            return original_renew(lease, lease_seconds=lease_seconds)
        with repository._tx() as conn:
            conn.execute(
                "UPDATE work_items SET lease_expires_at=? WHERE id=?",
                (time.time() - 1, lease.id),
            )
        successor.extend(repository.lease_work("replay", "successor"))
        return False

    monkeypatch.setattr(repository, "renew_work", lose_on_fence)

    result = await replay.replay_channel_history(123, "u1", dry_run=False)

    provider_call.assert_not_awaited()
    assert successor
    assert result["cursor_advanced_to"] == ""
    assert repository.work_rows("replay")[0]["lease_token"] == successor[0].token


@pytest.mark.asyncio
async def test_replay_repeated_cancel_drains_provider_once_and_acks_result(
    monkeypatch,
    tmp_state_dir,
):
    from services.mem0 import integrity, replay

    reset_repository_cache_for_tests()
    repository = get_repository()
    entered = threading.Event()
    release = threading.Event()
    side_effects = 0
    seen_keys = []

    class CancelService:
        async def replay(self, payload, _ingest):
            seen_keys.append(payload.get("idempotency_key"))

            def provider_side_effect():
                nonlocal side_effects
                entered.set()
                assert release.wait(timeout=2)
                side_effects += 1
                return {"kept": 1}

            return await asyncio.to_thread(provider_side_effect)

    monkeypatch.setattr(replay, "REPLAY_WORK_LEASE_SECONDS", 1.0)
    monkeypatch.setattr(replay, "REPLAY_WORK_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(integrity, "get_application_service", lambda: CancelService())
    monkeypatch.setattr(
        "services.mem0.history_index.get_lines_since",
        lambda _channel, _since, _limit: [
            {"ts": "t1", "role": "user", "author": "", "text": "Rust"},
            {"ts": "t2", "role": "assistant", "author": "", "text": "record"},
        ],
    )

    task = asyncio.create_task(
        replay.replay_channel_history(123, "u1", dry_run=False),
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert side_effects == 1
    assert len(seen_keys) == 1 and seen_keys[0]
    row = repository.work_rows("replay")[0]
    assert row["status"] == "acked"
    assert row["lease_owner"] is None
    assert repository.work_cursor("replay", "123") == "t2"
    assert not any(
        pending.get_name().startswith(
            (
                "mem0-replay-heartbeat-",
                "mem0-replay-provider-",
            )
        )
        for pending in asyncio.all_tasks()
        if pending is not asyncio.current_task()
    )


def test_canonical_visibility_is_strict_only_after_cutover(monkeypatch, tmp_state_dir):
    reset_repository_cache_for_tests()
    repository = get_repository()
    repository.create_fact(
        "u1",
        "conversation fact",
        {"scope": "conversation:10"},
        fact_id="canonical-1",
        legacy_id="legacy-1",
        project_vector=False,
    )

    assert canonical_fact_visible(
        "legacy-1",
        "u1",
        current_channel_id="10",
        mode="canonical",
    )
    assert not canonical_fact_visible(
        "legacy-1",
        "u1",
        current_channel_id="11",
        mode="canonical",
    )
    assert canonical_fact_visible("missing", "u1", mode="shadow")
    assert not canonical_fact_visible("missing", "u1", mode="canonical")

    repository.archive_fact("legacy-1", reason="stale")
    assert not canonical_fact_visible(
        "legacy-1",
        "u1",
        current_channel_id="10",
        mode="canonical",
    )


def test_search_visibility_uses_requested_user_not_provider_claim(
    monkeypatch,
    tmp_state_dir,
):
    from services.mem0.search import _is_fact_valid

    reset_repository_cache_for_tests()
    repository = get_repository()
    repository.create_fact(
        "u1",
        "owned",
        {},
        fact_id="canonical-1",
        legacy_id="legacy-1",
        project_vector=False,
    )
    monkeypatch.setenv("MEM0_INTEGRITY_MODE", "canonical")

    provider_row = {
        "id": "legacy-1",
        "memory": "owned",
        "user_id": "u2",
        "metadata": {"user_id": "u2"},
    }
    assert _is_fact_valid(provider_row, expected_user_id="u1")
    assert not _is_fact_valid(provider_row, expected_user_id="u2")
    assert not _is_fact_valid(provider_row)


@pytest.mark.asyncio
async def test_shadow_graph_uses_canonical_ids_and_resolves_legacy_sibling(
    monkeypatch,
    repository,
    projection_paths,
):
    from services.mem0 import integrity, priming
    from services.mem0.search import _graph_expand_candidates

    monkeypatch.setenv("MEM0_INTEGRITY_MODE", "shadow")
    monkeypatch.setattr(integrity, "get_repository", lambda path=None: repository)
    mem = FakeMem0()
    service = MemoryApplicationService(repository, mode="shadow")
    first = service.create_fact(
        mem,
        user_id="u1",
        text="Alice first",
        metadata={"scope": "global"},
        entities=[{"name": "Alice", "type": "person"}],
    )
    second = service.create_fact(
        mem,
        user_id="u1",
        text="Alice private",
        metadata={"scope": "conversation:10"},
        entities=[{"name": "Alice", "type": "person"}],
    )
    first_id = first["canonical_id"]
    second_id = second["canonical_id"]
    first_row = next(
        row
        for row in mem.rows.values()
        if row["metadata"].get("canonical_id") == first_id
    )
    second_row = next(
        row
        for row in mem.rows.values()
        if row["metadata"].get("canonical_id") == second_id
    )
    candidate = {**first_row, "score": 0.1}
    monkeypatch.setattr("services.mem0.client._get_mem0", lambda: mem)
    priming.clear()

    visible = await _graph_expand_candidates(
        [candidate],
        "u1",
        current_channel_id="10",
    )
    assert any(row.get("id") == second_row["id"] for row in visible)

    priming.clear()
    hidden_by_scope = await _graph_expand_candidates(
        [candidate],
        "u1",
        current_channel_id="11",
    )
    assert all(row.get("id") != second_row["id"] for row in hidden_by_scope)

    # Leave the graph projection deliberately stale. Canonical lifecycle
    # visibility must still prevent the sibling from being revived.
    repository.archive_fact(second_id, reason="test")
    priming.clear()
    hidden_after_archive = await _graph_expand_candidates(
        [candidate],
        "u1",
        current_channel_id="10",
    )
    assert all(row.get("id") != second_row["id"] for row in hidden_after_archive)

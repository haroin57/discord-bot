"""Additive relation provenance and retry fencing in isolated SQLite only."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")


@pytest.fixture(scope="module", autouse=True)
def bootstrap():
    import services.mem0 as package
    return package


@pytest.fixture
def graph(monkeypatch, tmp_path):
    from llm_mem0 import graph
    from llm_mem0.sqlite_store import SqliteStore
    monkeypatch.setenv("LLM_MEM0_STATE_DIR", str(tmp_path))
    store = SqliteStore("graph.sqlite", graph._SCHEMA)
    store.reset_path_for_test(str(tmp_path / "graph.sqlite"))
    monkeypatch.setattr(graph, "_store", store)
    monkeypatch.setattr(graph, "_top_entities_cache", {})
    monkeypatch.setattr(graph.time, "time", lambda: 1000)
    return graph


def endpoints(graph):
    return graph.upsert_entity("Alice", "person", "u"), graph.upsert_entity("Acme", "org", "u")


def add(graph, src, dst, source, **kwargs):
    return graph.add_relation(src, "works_at", dst, "u", source_fact_id=source, **kwargs)


def supports(graph, rid):
    return graph._store.read(lambda c: [dict(r) for r in c.execute(
        "SELECT * FROM entity_relation_sources WHERE relation_id=? ORDER BY source_fact_id", (rid,))])


def test_two_sources_share_relation_but_keep_independent_intervals(graph):
    a, b = endpoints(graph)
    first = add(graph, a, b, "first", valid_from=100)
    second = add(graph, a, b, "second", valid_from=200)
    assert first == second and first
    assert graph.get_relations(a)[0].source_fact_ids == ("first", "second")
    assert [r["valid_from"] for r in supports(graph, first)] == [100, 200]
    assert graph.expire_relations_for_fact("first", valid_to=300)
    active = graph.get_relations(a)
    assert len(active) == 1 and active[0].id == first
    assert active[0].source_fact_id == "second" and active[0].source_fact_ids == ("second",)
    assert active[0].valid_from == 200 and active[0].valid_to is None
    assert supports(graph, first)[0]["valid_to"] == 300


@pytest.mark.parametrize("deleted", ["first", "second"])
def test_deleting_one_source_preserves_other_evidence(graph, deleted):
    a, b = endpoints(graph)
    rid = add(graph, a, b, "first")
    assert add(graph, a, b, "second") == rid
    graph.add_edge(deleted, a)
    assert graph.delete_fact_edges(deleted) == 1
    remaining = "second" if deleted == "first" else "first"
    rows = graph.get_relations(a, only_valid=False)
    assert len(rows) == 1 and rows[0].source_fact_ids == (remaining,)
    assert rows[0].source_fact_id == remaining
    assert add(graph, a, b, deleted) == 0
    assert graph.get_relations(a)[0].source_fact_ids == (remaining,)
    graph.delete_fact_edges(remaining)
    assert graph.get_relations(a, only_valid=False) == []
    assert add(graph, a, b, remaining) == 0


def test_closed_source_open_retry_never_revives_or_extends_boundary(graph):
    a, b = endpoints(graph)
    rid = add(graph, a, b, "old", valid_from=100)
    assert graph.expire_relations_for_fact("old", valid_to=200)
    assert add(graph, a, b, "old", valid_from=100) == rid
    assert add(graph, a, b, "old", valid_from=100, valid_to=900) == rid
    assert graph.expire_relations_for_fact("old", valid_to=800)
    assert graph.get_relations(a) == []
    rows = graph.get_relations(a, only_valid=False)
    assert len(rows) == 1 and rows[0].valid_to == 200
    new = add(graph, a, b, "new", valid_from=300)
    assert new != rid and graph.get_relations(a)[0].id == new
    assert add(graph, a, b, "old") == rid
    assert graph.get_relations(a)[0].source_fact_ids == ("new",)


def test_closed_interval_first_projection_and_retry_remain_single_history_row(graph):
    a, b = endpoints(graph)
    rid = add(graph, a, b, "old", valid_from=100, valid_to=200)
    assert add(graph, a, b, "old", valid_from=100, valid_to=200) == rid
    assert add(graph, a, b, "old") == rid
    assert graph.get_relations(a) == []
    assert len(graph.get_relations(a, only_valid=False)) == 1


def test_source_retired_before_first_projection_cannot_become_open(graph):
    a, b = endpoints(graph)
    assert graph.expire_relations_for_fact("old", valid_to=200)
    rid = add(graph, a, b, "old", valid_from=100)
    assert rid and graph.get_relations(a) == []
    assert graph.get_relations(a, only_valid=False)[0].valid_to == 200
    graph.delete_fact_edges("deleted-before-projection")
    assert add(graph, a, b, "deleted-before-projection") == 0


def test_retirements_survive_store_reinitialization(graph):
    a, b = endpoints(graph)
    rid = add(graph, a, b, "old")
    assert graph.expire_relations_for_fact("old", valid_to=200)
    graph.delete_fact_edges("deleted")
    graph._store.reset_path_for_test(graph._store.resolve_path())
    assert add(graph, a, b, "old") == rid
    assert not graph.get_relations(a)
    assert add(graph, a, b, "deleted") == 0


def test_whole_relation_expiry_closes_every_source_and_preserves_history(graph):
    a, b = endpoints(graph)
    rid = add(graph, a, b, "first")
    add(graph, a, b, "second")
    assert graph.expire_relation(rid, valid_to=200)
    assert all(r["valid_to"] == 200 for r in supports(graph, rid))
    assert add(graph, a, b, "second") == rid
    assert graph.get_relations(a) == []
    assert not graph.expire_relation(rid, valid_to=300)
    assert graph.get_relations(a, only_valid=False)[0].valid_to == 200


def test_source_interval_gap_does_not_become_false_current_relation(graph, monkeypatch):
    a, b = endpoints(graph)
    rid = add(graph, a, b, "early", valid_from=100)
    assert add(graph, a, b, "later", valid_from=500) == rid
    graph.expire_relations_for_fact("early", valid_to=200)
    monkeypatch.setattr(graph.time, "time", lambda: 300)
    assert graph.get_relations(a) == []
    monkeypatch.setattr(graph.time, "time", lambda: 500)
    rows = graph.get_relations(a)
    assert len(rows) == 1 and rows[0].source_fact_ids == ("later",)
    assert rows[0].valid_from == 500


def test_unsourced_legacy_support_is_not_deleted_with_named_source(graph):
    a, b = endpoints(graph)
    rid = add(graph, a, b, None)
    assert add(graph, a, b, "named") == rid
    graph.delete_fact_edges("named")
    rows = graph.get_relations(a)
    assert len(rows) == 1 and rows[0].source_fact_id is None
    assert rows[0].source_fact_ids == ()


def test_existing_schema_is_extended_without_rewriting_old_rows(graph):
    # Build the previous schema directly; no real DB or destructive migration.
    old_schema = graph._SCHEMA.split("-- Additive provenance layer.")[0]
    with sqlite3.connect(graph._store.resolve_path()) as db:
        db.executescript(old_schema)
        db.executemany("INSERT INTO entities(id,canonical_name,type,user_id,created_at) VALUES (?,?,?,?,?)",
                       [(1, "Alice", "person", "u", 10), (2, "Acme", "org", "u", 10)])
        db.executemany("INSERT INTO entity_relations VALUES (?,?,?,?,?,?,?,?,?)", [
            (41, 1, "works_at", 2, "u", 10, 100, None, "legacy"),
            (42, 1, "knows", 2, "u", 10, 100, 200, "closed")])
        before = list(db.execute("SELECT * FROM entity_relations ORDER BY id"))
        objects = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert len(graph.get_relations(1, only_valid=False)) == 2
    with sqlite3.connect(graph._store.resolve_path()) as db:
        assert list(db.execute("SELECT * FROM entity_relations ORDER BY id")) == before
        after = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert all(after[name] == sql for name, sql in objects.items())
    assert add(graph, 1, 2, "new") == 41
    assert graph.expire_relations_for_fact("legacy", valid_to=300)
    assert graph.get_relations(1)[0].source_fact_ids == ("new",)
    assert graph.add_relation(1, "knows", 2, "u", source_fact_id="closed") == 42
    assert {r.id for r in graph.get_relations(1)} == {41}


def test_many_concurrent_sources_are_not_lost(graph):
    a, b = endpoints(graph)
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda i: add(graph, a, b, f"source-{i}"), range(40)))
    assert len(set(ids)) == 1 and ids[0]
    assert len(supports(graph, ids[0])) == 40
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: graph.delete_fact_edges(f"source-{i}"), range(20)))
    assert len(graph.get_relations(a)[0].source_fact_ids) == 20


@pytest.mark.parametrize("delete", [False, True])
def test_projection_racing_retirement_cannot_reopen_source(graph, delete):
    a, b = endpoints(graph)
    add(graph, a, b, "keep")
    barrier = threading.Barrier(2)
    def project():
        barrier.wait(timeout=3)
        return add(graph, a, b, "retired")
    def retire():
        barrier.wait(timeout=3)
        if delete:
            graph.delete_fact_edges("retired")
        else:
            graph.expire_relations_for_fact("retired", valid_to=200)
    with ThreadPoolExecutor(2) as pool:
        projects, retires = pool.submit(project), pool.submit(retire)
        projects.result(timeout=5)
        retires.result(timeout=5)
    assert graph.get_relations(a)[0].source_fact_ids == ("keep",)
    add(graph, a, b, "retired")
    assert graph.get_relations(a)[0].source_fact_ids == ("keep",)


def test_processes_share_source_transaction_without_losing_evidence(graph, tmp_path):
    a, b = endpoints(graph)
    code = """
import sys
from llm_mem0 import graph
graph._store.reset_path_for_test(sys.argv[1])
for i in range(8):
    assert graph.add_relation(int(sys.argv[2]), 'works_at', int(sys.argv[3]), 'u',
                              source_fact_id=sys.argv[4] + str(i))
"""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LLM_MEM0_STATE_DIR": str(tmp_path),
           "PYTHONPATH": str(Path(graph.__file__).resolve().parent.parent), "PYTHONDONTWRITEBYTECODE": "1"}
    children = [subprocess.Popen([sys.executable, "-B", "-c", code, graph._store.resolve_path(), str(a), str(b), str(i)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
    for child in children:
        _, error = child.communicate(timeout=30)
        assert child.returncode == 0, error.decode()
    assert len(graph.get_relations(a)[0].source_fact_ids) == 24


def test_owner_and_invalid_interval_guards_are_preserved(graph):
    a, b = endpoints(graph)
    outsider = graph.upsert_entity("Outsider", "person", "other")
    assert add(graph, a, outsider, "source") == 0
    assert add(graph, a, b, "source", valid_from=200, valid_to=100) == 0
    assert add(graph, a, b, "source", valid_to=True) == 0

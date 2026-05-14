"""Tests for services.mem0.graph — SQLite knowledge-graph layer."""

from __future__ import annotations

import os
import tempfile

import pytest

from services.mem0 import graph as graph_mod


@pytest.fixture(autouse=True)
def _temp_db(monkeypatch):
    """Point the graph module at a throw-away sqlite file per test."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    monkeypatch.setattr(graph_mod, "_db_path", tmp.name)
    yield
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


class TestUpsert:
    def test_upsert_creates_entity(self):
        eid = graph_mod.upsert_entity("Rust", "tech", "uid-1")
        assert eid > 0
        ent = graph_mod.find_entity("Rust", "uid-1")
        assert ent is not None
        assert ent.canonical_name == "Rust"
        assert ent.type == "tech"
        assert ent.mention_count == 1

    def test_upsert_increments_mention_count(self):
        a = graph_mod.upsert_entity("Rust", "tech", "uid-1")
        b = graph_mod.upsert_entity("Rust", "tech", "uid-1")
        assert a == b
        ent = graph_mod.find_entity("Rust", "uid-1")
        assert ent.mention_count == 2

    def test_aliases_stored(self):
        graph_mod.upsert_entity(
            "Namitape", "media", "uid-1", aliases=["ナミテープ", "namitape"],
        )
        # Alias lookup
        ent = graph_mod.find_entity("ナミテープ", "uid-1")
        assert ent is not None
        assert ent.canonical_name == "Namitape"

    def test_per_user_isolation(self):
        graph_mod.upsert_entity("Rust", "tech", "uid-A")
        graph_mod.upsert_entity("Rust", "tech", "uid-B")
        assert graph_mod.find_entity("Rust", "uid-A").id != graph_mod.find_entity("Rust", "uid-B").id

    def test_blank_name_returns_zero(self):
        assert graph_mod.upsert_entity("", "tech", "uid") == 0
        assert graph_mod.upsert_entity("Rust", "", "uid") == 0
        assert graph_mod.upsert_entity("Rust", "tech", "") == 0

    def test_normalize_collapses_whitespace(self):
        eid1 = graph_mod.upsert_entity("Rust", "tech", "uid")
        eid2 = graph_mod.upsert_entity("  Rust  ", "tech", "uid")
        eid3 = graph_mod.upsert_entity("Rust\nlang", "tech", "uid")
        # First two collapse to same canonical, third is different
        assert eid1 == eid2
        assert eid1 != eid3


class TestFind:
    def setup_method(self):
        # explicit fixture invocation happens per test in pytest, but
        # autouse fixture handles it for us — we just populate here.
        pass

    def test_find_missing_returns_none(self):
        assert graph_mod.find_entity("Nothing", "uid") is None

    def test_alias_resolution(self):
        graph_mod.upsert_entity("Rust", "tech", "uid", aliases=["rust-lang"])
        ent = graph_mod.find_entity("rust-lang", "uid")
        assert ent is not None and ent.canonical_name == "Rust"

    def test_fuzzy_prefix(self):
        graph_mod.upsert_entity("RustAsync", "tech", "uid")
        ent = graph_mod.find_entity("Rust", "uid", fuzzy=True)
        assert ent is not None
        assert ent.canonical_name == "RustAsync"

    def test_no_fuzzy(self):
        graph_mod.upsert_entity("RustAsync", "tech", "uid")
        assert graph_mod.find_entity("Rust", "uid", fuzzy=False) is None


class TestGetAliases:
    def test_returns_all_aliases(self):
        eid = graph_mod.upsert_entity(
            "CLANNAD", "media", "uid",
            aliases=["クラナド", "Clannad"],
        )
        aliases = graph_mod.get_aliases(eid)
        assert set(aliases) == {"クラナド", "Clannad"}

    def test_empty_for_unknown_entity(self):
        assert graph_mod.get_aliases(99999) == []

    def test_empty_for_falsy_id(self):
        assert graph_mod.get_aliases(0) == []


class TestEdges:
    def test_add_edge_and_related(self):
        eid = graph_mod.upsert_entity("Rust", "tech", "uid")
        assert graph_mod.add_edge("fact-1", eid)
        assert graph_mod.add_edge("fact-2", eid, role="subject")
        related = graph_mod.get_related_facts(eid, limit=10)
        # Both edges should surface; order depends on created_at granularity
        # (sub-second writes may tie, so we just check membership here).
        assert set(related) == {"fact-1", "fact-2"}

    def test_add_edge_idempotent(self):
        eid = graph_mod.upsert_entity("Rust", "tech", "uid")
        graph_mod.add_edge("fact-1", eid)
        graph_mod.add_edge("fact-1", eid)
        related = graph_mod.get_related_facts(eid, limit=10)
        assert related == ["fact-1"]

    def test_get_fact_entities(self):
        e1 = graph_mod.upsert_entity("Rust", "tech", "uid")
        e2 = graph_mod.upsert_entity("Namitape", "media", "uid")
        graph_mod.add_edge("fact-1", e1)
        graph_mod.add_edge("fact-1", e2)
        ents = graph_mod.get_fact_entities("fact-1")
        names = sorted(e.canonical_name for e in ents)
        assert names == ["Namitape", "Rust"]

    def test_delete_fact_edges(self):
        eid = graph_mod.upsert_entity("Rust", "tech", "uid")
        graph_mod.add_edge("fact-1", eid)
        graph_mod.add_edge("fact-2", eid)
        removed = graph_mod.delete_fact_edges("fact-1")
        assert removed == 1
        assert graph_mod.get_related_facts(eid) == ["fact-2"]

    def test_exclude_fact_ids(self):
        eid = graph_mod.upsert_entity("Rust", "tech", "uid")
        graph_mod.add_edge("fact-1", eid)
        graph_mod.add_edge("fact-2", eid)
        graph_mod.add_edge("fact-3", eid)
        related = graph_mod.get_related_facts(eid, limit=10, exclude_fact_ids=["fact-2"])
        assert set(related) == {"fact-1", "fact-3"}


class TestStats:
    def test_stats_empty(self):
        s = graph_mod.stats()
        assert s == {"entities": 0, "edges": 0, "aliases": 0}

    def test_stats_populated(self):
        e1 = graph_mod.upsert_entity("Rust", "tech", "uid", aliases=["rs"])
        e2 = graph_mod.upsert_entity("Namitape", "media", "uid")
        graph_mod.add_edge("f1", e1)
        graph_mod.add_edge("f1", e2)
        graph_mod.add_edge("f2", e1)
        s = graph_mod.stats("uid")
        assert s == {"entities": 2, "edges": 3, "aliases": 1}

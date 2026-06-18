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
        # `relations` field added in Phase C-1; zero on a fresh DB.
        assert s == {"entities": 0, "edges": 0, "aliases": 0, "relations": 0}

    def test_stats_populated(self):
        e1 = graph_mod.upsert_entity("Rust", "tech", "uid", aliases=["rs"])
        e2 = graph_mod.upsert_entity("Namitape", "media", "uid")
        graph_mod.add_edge("f1", e1)
        graph_mod.add_edge("f1", e2)
        graph_mod.add_edge("f2", e1)
        s = graph_mod.stats("uid")
        assert s == {"entities": 2, "edges": 3, "aliases": 1, "relations": 0}


class TestAliasAwareUpsert:
    """§1 of docs/mem0-dedup-issues.md — alias-canonical / canonical-alias merge."""

    def test_merges_when_new_alias_matches_existing_canonical(self):
        # Existing entity: canonical=ぼたん
        existing = graph_mod.upsert_entity("ぼたん", "media", "uid")
        # New event arrives as canonical=上伊那ぼたん with aliases=[ぼたん].
        # Should merge into the existing entity, not create a second row.
        merged = graph_mod.upsert_entity(
            "上伊那ぼたん", "media", "uid", aliases=["ぼたん"],
        )
        assert merged == existing
        # The new canonical surface form becomes an alias of the existing row.
        aliases = graph_mod.get_aliases(existing)
        assert "上伊那ぼたん" in aliases
        # Sanity: stats show one entity, not two
        s = graph_mod.stats("uid")
        assert s["entities"] == 1

    def test_merges_when_new_canonical_matches_existing_alias(self):
        # Existing entity has alias=ぼたん registered alongside canonical
        e1 = graph_mod.upsert_entity(
            "上伊那ぼたん", "media", "uid", aliases=["ぼたん"],
        )
        # Second event uses ぼたん as canonical — should merge to parent
        e2 = graph_mod.upsert_entity("ぼたん", "media", "uid")
        assert e1 == e2
        assert graph_mod.stats("uid")["entities"] == 1

    def test_mention_count_increments_on_alias_merge(self):
        e1 = graph_mod.upsert_entity("ぼたん", "media", "uid")
        graph_mod.upsert_entity(
            "上伊那ぼたん", "media", "uid", aliases=["ぼたん"],
        )
        ent = graph_mod.find_entity("ぼたん", "uid")
        assert ent.mention_count == 2

    def test_does_not_overwrite_canonical_on_merge(self):
        # Even when the merged-in surface form looks like a "better" canonical,
        # we keep the original canonical_name to avoid surprise renames.
        graph_mod.upsert_entity("ぼたん", "media", "uid")
        graph_mod.upsert_entity(
            "上伊那ぼたん", "media", "uid", aliases=["ぼたん"],
        )
        ent = graph_mod.find_entity("ぼたん", "uid")
        assert ent.canonical_name == "ぼたん"


class TestAppellationResolution:
    """§2 — 「先輩」/「お父さん」 collapse to their canonical proper nouns."""

    def test_senpai_resolves_to_haroin(self):
        # First write 'haroin' so its canonical row exists, then 先輩 should
        # land on the same entity via APPELLATION_MAP.
        haroin = graph_mod.upsert_entity("haroin", "person", "uid")
        senpai = graph_mod.upsert_entity("先輩", "person", "uid")
        assert haroin == senpai
        aliases = graph_mod.get_aliases(haroin)
        assert "先輩" in aliases

    def test_otosan_resolves_to_b0nchichi(self):
        b = graph_mod.upsert_entity("b0nchichi", "person", "uid")
        a = graph_mod.upsert_entity("お父さん", "person", "uid")
        assert a == b

    def test_appellation_without_prior_canonical_creates_canonical_row(self):
        # If 'haroin' was never registered, upserting 先輩 still creates a
        # canonical=haroin row (with 先輩 as alias), not a 先輩 canonical.
        eid = graph_mod.upsert_entity("先輩", "person", "uid")
        ent = graph_mod.find_entity("haroin", "uid")
        assert ent is not None and ent.id == eid
        assert "先輩" in graph_mod.get_aliases(eid)


class TestGetTopEntities:
    """§5a — top-N canonical names injected into the extract prompt."""

    def test_filters_below_mention_count_2(self):
        # Rust mentioned twice, Namitape only once
        graph_mod.upsert_entity("Rust", "tech", "uid")
        graph_mod.upsert_entity("Rust", "tech", "uid")
        graph_mod.upsert_entity("Namitape", "media", "uid")
        graph_mod.invalidate_top_entities_cache("uid")
        names = graph_mod.get_top_entities("uid")
        assert "Rust" in names
        assert "Namitape" not in names

    def test_orders_by_mention_count_desc(self):
        for _ in range(3):
            graph_mod.upsert_entity("Rust", "tech", "uid")
        for _ in range(5):
            graph_mod.upsert_entity("Namitape", "media", "uid")
        graph_mod.invalidate_top_entities_cache("uid")
        names = graph_mod.get_top_entities("uid")
        assert names[0] == "Namitape"
        assert names[1] == "Rust"

    def test_respects_limit(self):
        for i in range(5):
            graph_mod.upsert_entity(f"Ent{i}", "tech", "uid")
            graph_mod.upsert_entity(f"Ent{i}", "tech", "uid")
        graph_mod.invalidate_top_entities_cache("uid")
        names = graph_mod.get_top_entities("uid", limit=3)
        assert len(names) == 3

    def test_per_user_isolation(self):
        graph_mod.upsert_entity("Rust", "tech", "uid-A")
        graph_mod.upsert_entity("Rust", "tech", "uid-A")
        graph_mod.upsert_entity("Namitape", "media", "uid-B")
        graph_mod.upsert_entity("Namitape", "media", "uid-B")
        graph_mod.invalidate_top_entities_cache()
        assert graph_mod.get_top_entities("uid-A") == ["Rust"]
        assert graph_mod.get_top_entities("uid-B") == ["Namitape"]

    def test_cache_invalidate_picks_up_new_writes(self):
        graph_mod.upsert_entity("Rust", "tech", "uid")
        graph_mod.upsert_entity("Rust", "tech", "uid")
        graph_mod.invalidate_top_entities_cache("uid")
        assert graph_mod.get_top_entities("uid") == ["Rust"]
        # New entity hits 2 mentions — upsert_entity itself invalidates the cache
        graph_mod.upsert_entity("Namitape", "media", "uid")
        graph_mod.upsert_entity("Namitape", "media", "uid")
        names = graph_mod.get_top_entities("uid")
        assert set(names) == {"Rust", "Namitape"}

    def test_empty_user_returns_empty(self):
        assert graph_mod.get_top_entities("") == []
        assert graph_mod.get_top_entities("uid", limit=0) == []


# ---------------------------------------------------------------------------
# Phase C-1: entity_relations
# ---------------------------------------------------------------------------

class TestEntityRelations:
    def _setup_entities(self) -> tuple[int, int]:
        src = graph_mod.upsert_entity("先輩", "person", "uid-1")
        dst = graph_mod.upsert_entity("砺波いぶき", "person", "uid-1")
        return src, dst

    def test_add_relation_basic(self):
        src, dst = self._setup_entities()
        rid = graph_mod.add_relation(src, "loves", dst, "uid-1")
        assert rid > 0
        rels = graph_mod.get_relations(src, direction="src")
        assert len(rels) == 1
        r = rels[0]
        assert r.rel_type == "loves"
        assert r.dst_entity_id == dst
        assert r.valid_to is None

    def test_unique_constraint_blocks_duplicate_open_row(self):
        src, dst = self._setup_entities()
        rid1 = graph_mod.add_relation(src, "loves", dst, "uid-1")
        rid2 = graph_mod.add_relation(src, "loves", dst, "uid-1")
        assert rid1 > 0
        # INSERT OR IGNORE → rid2 is 0 because the row already exists.
        # Either way, only one currently-valid row should exist.
        rels = graph_mod.get_relations(src, only_valid=True)
        assert len(rels) == 1

    def test_expire_relation_sets_valid_to(self):
        src, dst = self._setup_entities()
        rid = graph_mod.add_relation(src, "loves", dst, "uid-1")
        ok = graph_mod.expire_relation(rid)
        assert ok is True
        rels_valid = graph_mod.get_relations(src, only_valid=True)
        assert rels_valid == []
        rels_all = graph_mod.get_relations(src, only_valid=False)
        assert len(rels_all) == 1
        assert rels_all[0].valid_to is not None

    def test_get_relations_by_type(self):
        src, dst = self._setup_entities()
        graph_mod.add_relation(src, "loves", dst, "uid-1")
        graph_mod.add_relation(src, "knows", dst, "uid-1")
        loves = graph_mod.get_relations(src, rel_type="loves")
        knows = graph_mod.get_relations(src, rel_type="knows")
        assert len(loves) == 1
        assert len(knows) == 1
        assert loves[0].rel_type == "loves"

    def test_get_relations_direction_src_and_dst(self):
        src, dst = self._setup_entities()
        graph_mod.add_relation(src, "loves", dst, "uid-1")
        src_view = graph_mod.get_relations(src, direction="src")
        dst_view = graph_mod.get_relations(dst, direction="dst")
        any_view = graph_mod.get_relations(src, direction="any")
        assert len(src_view) == 1
        assert len(dst_view) == 1
        assert len(any_view) == 1

    def test_per_user_isolation_for_relations(self):
        a_src = graph_mod.upsert_entity("Alice", "person", "uid-A")
        a_dst = graph_mod.upsert_entity("Bob", "person", "uid-A")
        b_src = graph_mod.upsert_entity("Alice", "person", "uid-B")
        b_dst = graph_mod.upsert_entity("Bob", "person", "uid-B")
        graph_mod.add_relation(a_src, "knows", a_dst, "uid-A")
        # uid-A has 1 relation; uid-B has none.
        assert len(graph_mod.get_relations(a_src)) == 1
        assert graph_mod.get_relations(b_src) == []

    def test_entity_delete_cascades_to_relations(self):
        src, dst = self._setup_entities()
        graph_mod.add_relation(src, "loves", dst, "uid-1")
        # Drop the dst entity directly via the underlying conn — simulates
        # an aggressive cleanup. ON DELETE CASCADE should remove the
        # relation row too.
        with graph_mod._conn() as c, graph_mod._db_lock:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("DELETE FROM entities WHERE id=?", (dst,))
            c.commit()
        rels = graph_mod.get_relations(src, only_valid=False)
        assert rels == []

    def test_stats_includes_relation_count(self):
        src, dst = self._setup_entities()
        graph_mod.add_relation(src, "loves", dst, "uid-1")
        s = graph_mod.stats("uid-1")
        assert s["relations"] == 1

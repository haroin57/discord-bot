"""Tests for scripts/merge_entities.py — duplicate-entity merge tool."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from services.mem0 import graph as graph_mod

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO / "scripts" / "merge_entities.py"


def _import_script():
    """Load scripts/merge_entities.py as a module (it lives outside sys.path)."""
    spec = importlib.util.spec_from_file_location("merge_entities", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def merge_mod():
    return _import_script()


@pytest.fixture(autouse=True)
def _temp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    monkeypatch.setattr(graph_mod, "_db_path", tmp.name)
    yield
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# list-candidates
# ---------------------------------------------------------------------------
class TestListCandidates:
    def test_detects_substring_containment(self, merge_mod):
        # Same type, one contains the other — must be flagged.
        graph_mod.upsert_entity("ぼたん", "media", "uid")
        graph_mod.upsert_entity("上伊那ぼたん", "media", "uid")
        from services.mem0.graph import _conn
        with _conn() as c:
            ents = merge_mod._list_entities(c, "uid")
            aliases = merge_mod._list_aliases(c, "uid")
        candidates = merge_mod._detect_candidates(ents, aliases)
        assert len(candidates) == 1
        group = candidates[0]
        assert {group["target_name"], *group["source_names"]} == {
            "ぼたん", "上伊那ぼたん",
        }
        assert "substring" in group["reason"]

    def test_detects_alias_canonical_collision(self, merge_mod):
        # entity A canonical=ぼたん, entity B canonical=上伊那ぼたん alias=ぼたん
        # would not be created post-alias-aware-upsert, so simulate the legacy
        # state directly via INSERT.
        from services.mem0.graph import _conn
        with _conn() as c:
            c.execute(
                "INSERT INTO entities(canonical_name,type,user_id,created_at,mention_count) "
                "VALUES (?,?,?,?,?)", ("ぼたん", "media", "uid", 0, 3),
            )
            c.execute(
                "INSERT INTO entities(canonical_name,type,user_id,created_at,mention_count) "
                "VALUES (?,?,?,?,?)", ("上伊那ぼたん", "media", "uid", 0, 1),
            )
            c.execute(
                "INSERT INTO entity_aliases(entity_id, alias) "
                "SELECT id, 'ぼたん' FROM entities WHERE canonical_name='上伊那ぼたん'",
            )
            c.commit()
            ents = merge_mod._list_entities(c, "uid")
            aliases = merge_mod._list_aliases(c, "uid")
        candidates = merge_mod._detect_candidates(ents, aliases)
        assert candidates and "alias-canonical" in candidates[0]["reason"]
        assert candidates[0]["confidence"] >= 0.95

    def test_no_candidates_for_distinct_entities(self, merge_mod):
        graph_mod.upsert_entity("Rust", "tech", "uid")
        graph_mod.upsert_entity("Namitape", "media", "uid")
        from services.mem0.graph import _conn
        with _conn() as c:
            ents = merge_mod._list_entities(c, "uid")
            aliases = merge_mod._list_aliases(c, "uid")
        assert merge_mod._detect_candidates(ents, aliases) == []

    def test_target_is_highest_mention_count(self, merge_mod):
        # Setup: ぼたん mentioned 5 times, 上伊那ぼたん only 2 → ぼたん is target.
        # We have to bypass the alias-aware upsert (it would merge them) by
        # crafting the rows directly.
        from services.mem0.graph import _conn
        with _conn() as c:
            c.execute(
                "INSERT INTO entities(canonical_name,type,user_id,created_at,mention_count) "
                "VALUES ('ぼたん','media','uid',0,5)",
            )
            c.execute(
                "INSERT INTO entities(canonical_name,type,user_id,created_at,mention_count) "
                "VALUES ('上伊那ぼたん','media','uid',0,2)",
            )
            c.commit()
            ents = merge_mod._list_entities(c, "uid")
            aliases = merge_mod._list_aliases(c, "uid")
        groups = merge_mod._detect_candidates(ents, aliases)
        assert groups[0]["target_name"] == "ぼたん"


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
class TestApply:
    def _setup_two_legacy_entities(self):
        """Create two un-merged entities the alias-aware upsert would have
        unified. We INSERT directly to bypass the new merge logic."""
        from services.mem0.graph import _conn
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO entities(canonical_name,type,user_id,created_at,mention_count) "
                "VALUES ('ぼたん','media','uid',0,3)",
            )
            target = cur.lastrowid
            cur = c.execute(
                "INSERT INTO entities(canonical_name,type,user_id,created_at,mention_count) "
                "VALUES ('上伊那ぼたん','media','uid',0,1)",
            )
            source = cur.lastrowid
            c.execute(
                "INSERT INTO entity_aliases(entity_id, alias) VALUES (?, '花川ぼたん')",
                (source,),
            )
            c.execute(
                "INSERT INTO fact_entity_edges(fact_id, entity_id, role, created_at) "
                "VALUES ('fact-target', ?, 'subject', 0)", (target,),
            )
            c.execute(
                "INSERT INTO fact_entity_edges(fact_id, entity_id, role, created_at) "
                "VALUES ('fact-source-only', ?, NULL, 0)", (source,),
            )
            # shared fact — both source and target link to it.
            c.execute(
                "INSERT INTO fact_entity_edges(fact_id, entity_id, role, created_at) "
                "VALUES ('fact-shared', ?, NULL, 0)", (target,),
            )
            c.execute(
                "INSERT INTO fact_entity_edges(fact_id, entity_id, role, created_at) "
                "VALUES ('fact-shared', ?, NULL, 0)", (source,),
            )
            c.commit()
        return target, source

    def test_apply_merges_edges_and_aliases(self, merge_mod):
        target, source = self._setup_two_legacy_entities()
        plan = [{"target_id": target, "source_ids": [source]}]
        totals = merge_mod._apply_plan(plan, dry_run=False)
        # source must be gone; target must have inherited everything
        from services.mem0.graph import _conn
        with _conn() as c:
            assert c.execute(
                "SELECT count(*) FROM entities WHERE id=?", (source,),
            ).fetchone()[0] == 0
            tgt = c.execute(
                "SELECT canonical_name, mention_count FROM entities WHERE id=?",
                (target,),
            ).fetchone()
            assert tgt["canonical_name"] == "ぼたん"
            assert tgt["mention_count"] == 3 + 1
            aliases = [
                r["alias"] for r in c.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_id=?",
                    (target,),
                ).fetchall()
            ]
            assert "上伊那ぼたん" in aliases
            assert "花川ぼたん" in aliases
            fact_ids = sorted(
                r["fact_id"] for r in c.execute(
                    "SELECT fact_id FROM fact_entity_edges WHERE entity_id=?",
                    (target,),
                ).fetchall()
            )
            assert fact_ids == ["fact-shared", "fact-source-only", "fact-target"]
            # source side fully cleared
            assert c.execute(
                "SELECT count(*) FROM fact_entity_edges WHERE entity_id=?",
                (source,),
            ).fetchone()[0] == 0
        assert totals["groups"] == 1
        assert totals["sources_merged"] == 1

    def test_apply_dry_run_does_not_persist(self, merge_mod):
        target, source = self._setup_two_legacy_entities()
        plan = [{"target_id": target, "source_ids": [source]}]
        totals = merge_mod._apply_plan(plan, dry_run=True)
        # row counts should still be reported
        assert totals["groups"] == 1
        # state unchanged
        from services.mem0.graph import _conn
        with _conn() as c:
            assert c.execute(
                "SELECT count(*) FROM entities WHERE id=?", (source,),
            ).fetchone()[0] == 1
            tgt_mc = c.execute(
                "SELECT mention_count FROM entities WHERE id=?", (target,),
            ).fetchone()[0]
            assert tgt_mc == 3  # unchanged

    def test_apply_rejects_cross_user(self, merge_mod):
        graph_mod.upsert_entity("Rust", "tech", "uid-A")
        graph_mod.upsert_entity("Rust", "tech", "uid-B")
        a = graph_mod.find_entity("Rust", "uid-A").id
        b = graph_mod.find_entity("Rust", "uid-B").id
        plan = [{"target_id": a, "source_ids": [b]}]
        with pytest.raises(Exception):
            merge_mod._apply_plan(plan, dry_run=False)


# ---------------------------------------------------------------------------
# orphan-cleanup
# ---------------------------------------------------------------------------
class TestOrphanCleanup:
    def test_deletes_only_zero_edge_entities(self, merge_mod):
        e_linked = graph_mod.upsert_entity("Linked", "tech", "uid")
        e_orphan = graph_mod.upsert_entity("Orphan", "tech", "uid")
        graph_mod.add_edge("fact-1", e_linked)
        stats = merge_mod._cleanup_orphans("uid", dry_run=False)
        assert stats == {"orphans": 1, "deleted": 1}
        assert graph_mod.find_entity("Orphan", "uid", fuzzy=False) is None
        assert graph_mod.find_entity("Linked", "uid", fuzzy=False) is not None

    def test_dry_run_reports_but_does_not_delete(self, merge_mod):
        graph_mod.upsert_entity("Orphan", "tech", "uid")
        stats = merge_mod._cleanup_orphans("uid", dry_run=True)
        assert stats == {"orphans": 1, "deleted": 0}
        assert graph_mod.find_entity("Orphan", "uid", fuzzy=False) is not None


# ---------------------------------------------------------------------------
# end-to-end CLI plan I/O
# ---------------------------------------------------------------------------
class TestCLIRoundtrip:
    def test_list_then_apply(self, merge_mod, tmp_path):
        # legacy duplicate
        from services.mem0.graph import _conn
        with _conn() as c:
            c.execute(
                "INSERT INTO entities(canonical_name,type,user_id,created_at,mention_count) "
                "VALUES ('ぼたん','media','uid',0,3)",
            )
            c.execute(
                "INSERT INTO entities(canonical_name,type,user_id,created_at,mention_count) "
                "VALUES ('上伊那ぼたん','media','uid',0,1)",
            )
            c.commit()
        plan_path = tmp_path / "plan.json"
        rc = merge_mod.main([
            "list-candidates", "--user-id", "uid", "--out", str(plan_path),
        ])
        assert rc == 0
        plan = json.loads(plan_path.read_text())
        assert plan["total_groups"] >= 1
        rc = merge_mod.main(["apply", "--plan", str(plan_path), "--dry-run"])
        assert rc == 0

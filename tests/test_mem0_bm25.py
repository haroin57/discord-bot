"""Unit tests for services/mem0/bm25_index.py and the hybrid search path.

The store is a per-test tempfile pointed at by _reset_db_path_for_test.
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_bm25_db():
    """Per-test tempfile FTS5 db. Module-level state is reset after."""
    from services.mem0 import bm25_index
    fd, path = tempfile.mkstemp(suffix=".sqlite", prefix="bm25_test_")
    os.close(fd)
    bm25_index._reset_db_path_for_test(path)
    yield path
    bm25_index._reset_db_path_for_test(None)
    # Clean up sqlite + WAL/SHM siblings.
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# index_fact / search_bm25 basics
# ---------------------------------------------------------------------------

class TestIndexAndSearch:
    def test_index_and_search_basic(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        assert bm25_index.index_fact("f1", "Rust async is great", "userA")
        assert bm25_index.index_fact("f2", "TypeScript is fine", "userA")
        hits = bm25_index.search_bm25("Rust", "userA", limit=10)
        ids = [h["id"] for h in hits]
        assert "f1" in ids
        assert "f2" not in ids

    def test_per_user_isolation(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        bm25_index.index_fact("f1", "Rust async is great", "userA")
        bm25_index.index_fact("f2", "Rust async is great", "userB")
        hits_a = bm25_index.search_bm25("Rust", "userA", limit=10)
        hits_b = bm25_index.search_bm25("Rust", "userB", limit=10)
        assert [h["id"] for h in hits_a] == ["f1"]
        assert [h["id"] for h in hits_b] == ["f2"]

    def test_index_idempotent_on_fact_id(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        # The two texts share no tokens — that way searching for a term in
        # the first text must miss after the row is replaced.
        bm25_index.index_fact("f1", "alpha apple", "userA")
        bm25_index.index_fact("f1", "beta banana", "userA")
        # Searching for "alpha" should now miss (the row was replaced).
        first_hits = bm25_index.search_bm25("alpha", "userA", limit=10)
        # search for "banana" matches the replacement
        second_hits = bm25_index.search_bm25("banana", "userA", limit=10)
        assert not any(h["id"] == "f1" for h in first_hits)
        assert any(h["id"] == "f1" for h in second_hits)
        # Also assert there's only one row for f1 (no duplication).
        stats = bm25_index.stats()
        assert stats["rows"] == 1

    def test_remove_fact_after_delete(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        bm25_index.index_fact("f1", "deletable text", "userA")
        removed = bm25_index.remove_fact("f1")
        assert removed >= 1
        hits = bm25_index.search_bm25("deletable", "userA", limit=10)
        assert not any(h["id"] == "f1" for h in hits)

    def test_morpho_handles_japanese(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        bm25_index.index_fact("f1", "先輩は砺波いぶきが好き", "userA")
        bm25_index.index_fact("f2", "TypeScript の話を昨日した", "userA")
        # Morpho splits "砺波いぶき" into two content tokens with the
        # default Janome dictionary; we still want a hit when querying
        # for the canonical compound name (one of the tokens matches).
        hits = bm25_index.search_bm25("砺波いぶき", "userA", limit=10)
        ids = [h["id"] for h in hits]
        assert "f1" in ids
        assert "f2" not in ids

    def test_empty_query_returns_empty(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        bm25_index.index_fact("f1", "text", "userA")
        assert bm25_index.search_bm25("", "userA") == []
        assert bm25_index.search_bm25("   ", "userA") == []

    def test_invalid_inputs_are_no_ops(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        assert bm25_index.index_fact("", "text", "userA") is False
        assert bm25_index.index_fact("f1", "", "userA") is False
        assert bm25_index.index_fact("f1", "text", "") is False

    def test_natural_language_query_finds_proper_noun(self, fresh_bm25_db):
        """Regression for the 2026-05-23 incident: long natural-language
        queries used to phrase-match the whole sentence and return 0 hits.
        Morpho tokenization splits the question into content words so the
        proper noun inside it still matches."""
        from services.mem0 import bm25_index
        bm25_index.index_fact("f2", "好きな食べ物はラーメン", "userA")
        bm25_index.index_fact("f3", "上伊那ぼたんを3巻まで読んだ", "userA")
        hits = bm25_index.search_bm25(
            "上伊那ぼたんのキャラクターで誰が一番好き？", "userA", limit=10,
        )
        assert any(h["id"] == "f3" for h in hits)

    def test_query_with_fts5_special_chars_does_not_raise(self, fresh_bm25_db):
        """Parens, asterisks, colons used to raise OperationalError."""
        from services.mem0 import bm25_index
        bm25_index.index_fact("f1", "Rust is great", "userA")
        # Should not raise; result may be empty or contain f1.
        hits = bm25_index.search_bm25("Rust (great): *", "userA", limit=5)
        assert isinstance(hits, list)

    def test_morpho_query_drops_particles(self, fresh_bm25_db):
        """Particles ('の' 'は' 'が' 'で') and 1-char nouns should not appear
        in the FTS5 query — only content words remain after morpho."""
        from services.mem0 import bm25_index
        bm25_index.index_fact("f1", "先輩は Rust が好き", "userA")
        # Question with multiple particles. Should still find f1 via the
        # 'Rust' / '好き' content words.
        hits = bm25_index.search_bm25("先輩はRustのことが好き？", "userA", limit=5)
        assert any(h["id"] == "f1" for h in hits)


# ---------------------------------------------------------------------------
# rrf_merge
# ---------------------------------------------------------------------------

class TestRrfMerge:
    def test_orders_by_combined_rank(self):
        from services.mem0.bm25_index import rrf_merge

        # f1 at vector rank 1, bm25 rank 5
        # f2 at vector rank 5, bm25 rank 1
        # f3 at vector rank 2 only
        vector = [
            {"id": "f1", "score": 0.1},
            {"id": "f3", "score": 0.2},
            {"id": "fx", "score": 0.3},
            {"id": "fy", "score": 0.4},
            {"id": "f2", "score": 0.5},
        ]
        bm25 = [
            {"id": "f2", "rank": 1},
            {"id": "fz", "rank": 2},
            {"id": "fw", "rank": 3},
            {"id": "fv", "rank": 4},
            {"id": "f1", "rank": 5},
        ]
        merged = rrf_merge(vector, bm25, k=60, limit=5)
        ids = [m["id"] for m in merged]
        # f1 and f2 should both rank high (appear in both lists).
        assert ids[0] in ("f1", "f2")
        assert ids[1] in ("f1", "f2")
        # All entries have an rrf_score.
        for m in merged:
            assert "rrf_score" in m

    def test_handles_empty_one_side(self):
        from services.mem0.bm25_index import rrf_merge

        vector = [{"id": "f1", "score": 0.1}, {"id": "f2", "score": 0.2}]
        merged = rrf_merge(vector, [], k=60, limit=10)
        assert {m["id"] for m in merged} == {"f1", "f2"}

        bm25 = [{"id": "f3", "rank": 1}]
        merged = rrf_merge([], bm25, k=60, limit=10)
        assert [m["id"] for m in merged] == ["f3"]

    def test_handles_empty_both_sides(self):
        from services.mem0.bm25_index import rrf_merge

        merged = rrf_merge([], [], k=60, limit=10)
        assert merged == []

    def test_keeps_cosine_score_when_present(self):
        from services.mem0.bm25_index import rrf_merge

        vector = [{"id": "f1", "score": 0.42}]
        bm25 = [{"id": "f1", "rank": 1}]
        merged = rrf_merge(vector, bm25, k=60, limit=10)
        assert merged[0]["score"] == 0.42

    def test_neutral_score_for_bm25_only(self):
        from services.mem0.bm25_index import rrf_merge

        bm25 = [{"id": "f1", "rank": 1, "memory": "text"}]
        merged = rrf_merge([], bm25, k=60, limit=10)
        # BM25-only hits get a neutral placeholder so downstream filters
        # don't drop them. Currently 0.8 (matching graph_expand).
        assert merged[0].get("score") == 0.8


# ---------------------------------------------------------------------------
# search_memories_hybrid
# ---------------------------------------------------------------------------

class TestSearchMemoriesHybrid:
    @pytest.mark.asyncio
    async def test_falls_back_to_vector_when_disabled(
        self, monkeypatch, fresh_bm25_db,
    ):
        # Patch the env-driven flag inline by monkeypatching the module
        # config-import side-effect: simulate hybrid=False.
        import services.mem0.search as search_mod

        async def fake_search(query, user_id, limit=8):
            return [{"id": "f1", "score": 0.1, "memory": "vec hit"}]

        monkeypatch.setattr(search_mod, "search_memories", fake_search)

        import config
        monkeypatch.setattr(config, "MEM0_HYBRID_ENABLED", False)

        result = await search_mod.search_memories_hybrid("q", "u", limit=5)
        assert [r["id"] for r in result] == ["f1"]

    @pytest.mark.asyncio
    async def test_merges_vector_and_bm25(self, monkeypatch, fresh_bm25_db):
        import services.mem0.search as search_mod

        async def fake_search(query, user_id, limit=8):
            return [{"id": "vec1", "score": 0.1, "memory": "vec hit"}]

        monkeypatch.setattr(search_mod, "search_memories", fake_search)

        from services.mem0 import bm25_index
        bm25_index.index_fact("bm1", "exact term match", "u")

        import config
        monkeypatch.setattr(config, "MEM0_HYBRID_ENABLED", True)
        monkeypatch.setattr(config, "MEM0_BM25_RRF_K", 60, raising=False)
        monkeypatch.setattr(config, "MEM0_BM25_LIMIT", 20, raising=False)

        result = await search_mod.search_memories_hybrid(
            "exact term", "u", limit=10,
        )
        ids = {r["id"] for r in result}
        assert "vec1" in ids
        assert "bm1" in ids

    @pytest.mark.asyncio
    async def test_survives_vector_exception(self, monkeypatch, fresh_bm25_db):
        import services.mem0.search as search_mod

        async def boom(*args, **kwargs):
            raise RuntimeError("vector broke")

        monkeypatch.setattr(search_mod, "search_memories", boom)

        from services.mem0 import bm25_index
        bm25_index.index_fact("bm1", "fallback content", "u")

        import config
        monkeypatch.setattr(config, "MEM0_HYBRID_ENABLED", True)
        monkeypatch.setattr(config, "MEM0_BM25_RRF_K", 60, raising=False)
        monkeypatch.setattr(config, "MEM0_BM25_LIMIT", 20, raising=False)

        result = await search_mod.search_memories_hybrid(
            "fallback", "u", limit=10,
        )
        assert [r["id"] for r in result] == ["bm1"]


# ---------------------------------------------------------------------------
# reindex_all (used by scripts/build_mem0_bm25_index.py)
# ---------------------------------------------------------------------------

class TestReindex:
    def test_reindex_all_replaces_existing(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        bm25_index.index_fact("old1", "should be removed", "u")
        count = bm25_index.reindex_all([
            ("f1", "first", "u"),
            ("f2", "second", "u"),
        ])
        assert count == 2
        hits = bm25_index.search_bm25("removed", "u", limit=10)
        assert hits == []
        hits = bm25_index.search_bm25("first", "u", limit=10)
        assert [h["id"] for h in hits] == ["f1"]

    def test_reindex_all_skips_empty_rows(self, fresh_bm25_db):
        from services.mem0 import bm25_index

        count = bm25_index.reindex_all([
            ("", "no id", "u"),
            ("f1", "", "u"),
            ("f2", "valid", "u"),
            ("f3", "valid", ""),
        ])
        assert count == 1
        stats = bm25_index.stats()
        assert stats["rows"] == 1

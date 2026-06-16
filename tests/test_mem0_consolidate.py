"""Unit tests for offline Mem0 consolidation (ADR-0016)."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np
import pytest

import services.mem0.dedup as dedup_mod

_REPO = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "mem0_consolidate", _REPO / "scripts" / "mem0_consolidate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CONS = _load_script()


# ---------------------------------------------------------------------------
# synthesize_cluster (dedup.py)
# ---------------------------------------------------------------------------
class TestSynthesizeCluster:
    @pytest.mark.asyncio
    async def test_returns_none_below_two(self):
        assert await dedup_mod.synthesize_cluster(["only one"]) is None
        assert await dedup_mod.synthesize_cluster([]) is None

    @pytest.mark.asyncio
    async def test_consolidates_via_llm(self, monkeypatch):
        # Mock the LLM round-trip to return a 2→1 consolidation.
        async def _client():
            return object()
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", _client)

        async def _create(*a, **k):
            return object()
        monkeypatch.setattr("services.mem0.cli_compat.cli_messages_create", _create)
        monkeypatch.setattr(
            "services.mem0.cli_compat.extract_text",
            lambda r: '{"consolidated": ["統合後の1件"], "note": "ok"}',
        )
        out = await dedup_mod.synthesize_cluster(["A好き", "Aが好き"])
        assert out == ["統合後の1件"]

    @pytest.mark.asyncio
    async def test_noop_when_not_reduced(self, monkeypatch):
        async def _client():
            return object()
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", _client)
        monkeypatch.setattr("services.mem0.cli_compat.cli_messages_create", AsyncMock(return_value=object()))
        # LLM returned same count (2 in, 2 out) → no-op skip.
        monkeypatch.setattr(
            "services.mem0.cli_compat.extract_text",
            lambda r: '{"consolidated": ["x", "y"]}',
        )
        assert await dedup_mod.synthesize_cluster(["a", "b"]) is None

    @pytest.mark.asyncio
    async def test_fail_open_on_error(self, monkeypatch):
        async def _raise():
            raise RuntimeError("net")
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", _raise)
        assert await dedup_mod.synthesize_cluster(["a", "b"]) is None


# ---------------------------------------------------------------------------
# greedy_clusters
# ---------------------------------------------------------------------------
class TestGreedyClusters:
    def _facts(self, cats):
        return [{"metadata": {"category": c}} for c in cats]

    def test_groups_near_duplicates(self):
        # 3 vectors: A and A' close, B far.
        embs = np.array([[1, 0], [0.99, 0.01], [0, 1]], dtype=np.float32)
        facts = self._facts([None, None, None])
        clusters = CONS.greedy_clusters(facts, embs, sim_threshold=0.9, max_cluster=8)
        assert len(clusters) == 1
        assert set(clusters[0]) == {0, 1}

    def test_category_filter_splits(self):
        embs = np.array([[1, 0], [0.999, 0.001]], dtype=np.float32)
        facts = self._facts(["tech", "hobby"])
        clusters = CONS.greedy_clusters(facts, embs, sim_threshold=0.9, max_cluster=8)
        assert clusters == []  # different categories never cluster

    def test_max_cluster_cap(self):
        embs = np.array([[1, 0]] * 10, dtype=np.float32)
        facts = self._facts([None] * 10)
        clusters = CONS.greedy_clusters(facts, embs, sim_threshold=0.9, max_cluster=3)
        assert all(len(c) <= 3 for c in clusters)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_is_archived(self):
        import time as _t
        assert CONS._is_archived({"consolidated_into": "x"}) is True
        assert CONS._is_archived({"valid_to": int(_t.time()) - 10}) is True
        assert CONS._is_archived({"valid_to": int(_t.time()) + 9999}) is False
        assert CONS._is_archived({}) is False

    def test_merge_metadata_aggregates(self):
        cluster = [
            {"metadata": {"category": "hobby", "importance": 3, "mention_count": 2}},
            {"metadata": {"category": "hobby", "importance": 4, "mention_count": 1}},
            {"metadata": {"category": "tech", "importance": 2, "mention_count": 5}},
        ]
        m = CONS._merge_metadata(cluster)
        assert m["importance"] == 4          # max
        assert m["mention_count"] == 8       # sum
        assert m["category"] == "hobby"      # majority
        assert m["consolidated_from"] == 3
        assert m["source"] == "consolidate"


class TestExactDupFastPath:
    """Exact-dup clusters keep text deterministically without an LLM call."""

    @pytest.mark.asyncio
    async def test_run_uses_fast_path_for_identical(self, monkeypatch):
        # Two byte-identical facts in one cluster → no synthesize_cluster call.
        facts = [
            {"id": "a", "memory": "Xが好き", "metadata": {"category": "hobby"}},
            {"id": "b", "memory": "Xが好き", "metadata": {"category": "hobby"}},
        ]

        class _Mem:
            def get_all(self, user_id, limit): return {"results": facts}
        # run() imports these inside the function, so patch at source modules.
        monkeypatch.setattr("services.mem0.client._get_mem0", lambda: _Mem())

        # identical embeddings → one cluster
        class _Emb:
            def create(self, model, input):
                class R: pass
                r = R(); r.data = [type("D", (), {"embedding": [1.0, 0.0]})()
                                   for _ in input]
                return r
        class _OAI:
            embeddings = _Emb()
        import openai
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: _OAI())

        synth = AsyncMock()
        monkeypatch.setattr("services.mem0.dedup.synthesize_cluster", synth)

        import tempfile, os
        out = os.path.join(tempfile.gettempdir(), "consol_test.md")
        args = type("A", (), {"user_id": "u", "sim": 0.9, "apply": False, "out": out})()
        await CONS.run(args)
        synth.assert_not_awaited()   # fast path: LLM never called
        report = open(out).read()
        assert "exact_dup: 1" in report

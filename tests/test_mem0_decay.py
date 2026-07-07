"""Tests for services.mem0.decay — Phase ② tier decay + gist compression."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.mem0 import decay


class TestActivationToTier:
    def test_live(self):
        assert decay.activation_to_tier(-1.0) == 0

    def test_hot(self):
        assert decay.activation_to_tier(-7.0) == 1

    def test_warm(self):
        assert decay.activation_to_tier(-8.0) == 2

    def test_cold(self):
        assert decay.activation_to_tier(-10.0) == 3

    def test_neg_inf_is_cold(self):
        assert decay.activation_to_tier(float("-inf")) == 3


class TestIsGistExempt:
    def test_high_importance_exempt(self):
        assert decay.is_gist_exempt({"importance": 5}) is True

    def test_low_importance_not_exempt(self):
        assert decay.is_gist_exempt({"importance": 2}) is False

    def test_attribute_tagged_exempt(self):
        assert decay.is_gist_exempt({"importance": 2, "attribute": "residence"}) is True

    def test_already_gist_exempt(self):
        assert decay.is_gist_exempt({"importance": 2, "gist_of": "a,b,c"}) is True


class TestRecomputeTiersForUser:
    @pytest.mark.asyncio
    async def test_writes_changed_tiers_only(self, monkeypatch):
        now = time.time()
        rows = [
            {  # stale — was tier=0 (Live) but hasn't been touched in 2 years
                "id": "f1", "memory": "古い話",
                "metadata": {"tier": 0, "timestamp": int(now - 86400 * 730), "mention_count": 1},
            },
            {  # freshly reinforced — already correctly tier=0
                "id": "f2", "memory": "最近の話",
                "metadata": {"tier": 0, "timestamp": int(now - 60), "mention_count": 1},
            },
            {  # archived — must be skipped entirely
                "id": "f3", "memory": "アーカイブ済み",
                "metadata": {"tier": 0, "timestamp": int(now - 86400 * 365), "valid_to": int(now - 1)},
            },
        ]
        mem = MagicMock()
        monkeypatch.setattr("services.mem0.client._get_mem0", lambda: mem)
        monkeypatch.setattr(
            "services.mem0.client.get_all_memories", AsyncMock(return_value=rows),
        )
        monkeypatch.setattr("services.mem0.client._refresh_mem0_client_token", lambda: None)
        monkeypatch.setattr("services.oauth_manager.ensure_valid_token", AsyncMock())

        summary = await decay.recompute_tiers_for_user("u1", dry_run=False)

        assert summary["checked"] == 2  # f3 excluded (archived)
        assert summary["changed"] == 1  # only f1 transitions
        mem.update.assert_called_once()
        assert mem.update.call_args.kwargs["memory_id"] == "f1"
        assert mem.update.call_args.kwargs["metadata"]["tier"] == 3

    @pytest.mark.asyncio
    async def test_dry_run_never_writes(self, monkeypatch):
        now = time.time()
        rows = [{
            "id": "f1", "memory": "古い話",
            "metadata": {"tier": 0, "timestamp": int(now - 86400 * 730), "mention_count": 1},
        }]
        mem = MagicMock()
        monkeypatch.setattr("services.mem0.client._get_mem0", lambda: mem)
        monkeypatch.setattr(
            "services.mem0.client.get_all_memories", AsyncMock(return_value=rows),
        )

        summary = await decay.recompute_tiers_for_user("u1", dry_run=True)

        assert summary["changed"] == 1  # "would change"
        mem.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_mem_returns_empty_summary(self, monkeypatch):
        monkeypatch.setattr("services.mem0.client._get_mem0", lambda: None)
        summary = await decay.recompute_tiers_for_user("u1", dry_run=True)
        assert summary == {"checked": 0, "changed": 0, "transitions": {}}


class TestGistCompressForUser:
    @pytest.mark.asyncio
    async def test_clusters_by_entity_and_archives_originals(self, monkeypatch):
        rows = [
            {"id": f"f{i}", "memory": f"Rust の話 {i}",
             "metadata": {"tier": 3, "importance": 2, "category": "tech"}}
            for i in range(3)
        ] + [
            {"id": "exempt1", "memory": "体重58kg",
             "metadata": {"tier": 3, "importance": 5}},  # exempt: imp>3
        ]
        mem = MagicMock()
        mem.add.return_value = {"results": [{"id": "gist1"}]}
        monkeypatch.setattr("services.mem0.client._get_mem0", lambda: mem)
        monkeypatch.setattr(
            "services.mem0.client.get_all_memories", AsyncMock(return_value=rows),
        )
        monkeypatch.setattr("services.mem0.client._refresh_mem0_client_token", lambda: None)
        monkeypatch.setattr("services.oauth_manager.ensure_valid_token", AsyncMock())

        entity = MagicMock(id=1, mention_count=5)

        def _fake_get_fact_entities(fact_id):
            return [entity] if fact_id.startswith("f") else []

        monkeypatch.setattr("services.mem0.graph.get_fact_entities", _fake_get_fact_entities)
        monkeypatch.setattr("services.mem0.graph.add_edge", MagicMock(return_value=True))

        async def _fake_synthesize_cluster(texts):
            assert len(texts) == 3
            return ["Rust の話をまとめて3回した"]

        monkeypatch.setattr(
            "services.mem0.dedup.synthesize_cluster", _fake_synthesize_cluster,
        )

        summary = await decay.gist_compress_for_user("u1", dry_run=False)

        assert summary["clusters"] == 1
        assert summary["compressed"] == 1
        assert summary["archived"] == 3
        archived_ids = {c.kwargs["memory_id"] for c in mem.update.call_args_list}
        assert archived_ids == {"f0", "f1", "f2"}
        for c in mem.update.call_args_list:
            assert c.kwargs["metadata"]["valid_to"]

    @pytest.mark.asyncio
    async def test_dry_run_never_writes(self, monkeypatch):
        rows = [
            {"id": f"f{i}", "memory": f"Rust の話 {i}",
             "metadata": {"tier": 3, "importance": 2}}
            for i in range(3)
        ]
        mem = MagicMock()
        monkeypatch.setattr("services.mem0.client._get_mem0", lambda: mem)
        monkeypatch.setattr(
            "services.mem0.client.get_all_memories", AsyncMock(return_value=rows),
        )
        entity = MagicMock(id=1, mention_count=5)
        monkeypatch.setattr(
            "services.mem0.graph.get_fact_entities", lambda fid: [entity],
        )

        async def _fake_synthesize_cluster(texts):
            return ["まとめ"]

        monkeypatch.setattr(
            "services.mem0.dedup.synthesize_cluster", _fake_synthesize_cluster,
        )

        summary = await decay.gist_compress_for_user("u1", dry_run=True)

        assert summary["clusters"] == 1
        mem.update.assert_not_called()
        mem.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_mem_returns_empty_summary(self, monkeypatch):
        monkeypatch.setattr("services.mem0.client._get_mem0", lambda: None)
        summary = await decay.gist_compress_for_user("u1", dry_run=True)
        assert summary == {"clusters": 0, "compressed": 0, "archived": 0}

"""Tests for the Phase C-2 extract_full_signals + ingest relation hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_anthropic_resp(json_text: str):
    content_block = MagicMock()
    content_block.text = json_text
    resp = MagicMock()
    resp.content = [content_block]
    return resp


class TestExtractFullSignals:
    @pytest.mark.asyncio
    async def test_returns_facts_and_relations(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"facts":[{"speaker":"self","text":"先輩は砺波いぶきが好き",'
                '"importance":3,"category":"hobby","subcategory":"vtuber",'
                '"tags":["botanica"],"entities":[]}],'
                '"relations":[{"src":"haroin","rel_type":"loves","dst":"砺波いぶき"}]}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        monkeypatch.setattr(
            "services.mem0.extract._format_known_entities",
            lambda _uid: "(none)",
        )
        from services.mem0.extract import extract_full_signals
        out = await extract_full_signals("先輩はいぶきが好き", "へえ")
        assert len(out["facts"]) == 1
        assert out["facts"][0]["text"] == "先輩は砺波いぶきが好き"
        assert out["relations"] == [
            {"src": "haroin", "rel_type": "loves", "dst": "砺波いぶき"},
        ]

    @pytest.mark.asyncio
    async def test_drops_unknown_rel_types(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"facts":[],"relations":['
                '{"src":"a","rel_type":"explodes","dst":"b"},'
                '{"src":"a","rel_type":"loves","dst":"b"}]}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        monkeypatch.setattr(
            "services.mem0.extract._format_known_entities",
            lambda _uid: "(none)",
        )
        from services.mem0.extract import extract_full_signals
        out = await extract_full_signals("x", "y")
        rel_types = [r["rel_type"] for r in out["relations"]]
        assert "loves" in rel_types
        assert "explodes" not in rel_types

    @pytest.mark.asyncio
    async def test_drops_self_loops(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"facts":[],"relations":['
                '{"src":"a","rel_type":"loves","dst":"a"}]}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        monkeypatch.setattr(
            "services.mem0.extract._format_known_entities",
            lambda _uid: "(none)",
        )
        from services.mem0.extract import extract_full_signals
        out = await extract_full_signals("x", "y")
        assert out["relations"] == []

    @pytest.mark.asyncio
    async def test_caps_at_5_relations(self, monkeypatch):
        big = ",".join(
            f'{{"src":"a{i}","rel_type":"loves","dst":"b{i}"}}'
            for i in range(10)
        )
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"facts":[],"relations":[' + big + ']}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        monkeypatch.setattr(
            "services.mem0.extract._format_known_entities",
            lambda _uid: "(none)",
        )
        from services.mem0.extract import extract_full_signals
        out = await extract_full_signals("x", "y")
        assert len(out["relations"]) == 5

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_client(self, monkeypatch):
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=None),
        )
        from services.mem0.extract import extract_full_signals
        out = await extract_full_signals("a", "b")
        assert out == {"facts": [], "relations": []}


class TestExtractFactsBackwardsCompat:
    @pytest.mark.asyncio
    async def test_returns_only_facts_list(self, monkeypatch):
        from services.mem0 import extract as extract_mod

        async def _fake_signals(user_text, assistant_text, context_hint="", user_id=None):
            return {
                "facts": [{"speaker": "self", "text": "x"}],
                "relations": [{"src": "a", "rel_type": "loves", "dst": "b"}],
            }
        monkeypatch.setattr(extract_mod, "extract_full_signals", _fake_signals)
        out = await extract_mod.extract_facts_for_self("u", "a")
        # Wrapper returns only the facts list, not the dict with relations.
        assert isinstance(out, list)
        assert out == [{"speaker": "self", "text": "x"}]


USER_ID = "user-123"


class TestIngestRelations:
    @pytest.fixture(autouse=True)
    def _bm25_stub(self, monkeypatch):
        # Stub bm25 index hook so insert path doesn't touch the real sqlite.
        from services.mem0 import bm25_index
        monkeypatch.setattr(bm25_index, "index_fact", lambda *a, **k: True)

    @pytest.mark.asyncio
    async def test_relations_added_when_entities_known(self, monkeypatch):
        from services.mem0 import graph as graph_mod
        # Fresh in-memory graph for this test
        import tempfile
        import os
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.close()
        monkeypatch.setattr(graph_mod, "_db_path", tmp.name)
        try:
            # Pre-create the entities the relation will point at.
            src = graph_mod.upsert_entity("haroin", "person", USER_ID)
            dst = graph_mod.upsert_entity("砺波いぶき", "person", USER_ID)
            assert src and dst

            facts = [{
                "speaker": "self",
                "text": "先輩は砺波いぶきが好き",
                "importance": 3,
                "category": "hobby",
                "subcategory": "vtuber",
                "tags": [],
            }]
            relations = [{"src": "haroin", "rel_type": "loves", "dst": "砺波いぶき"}]

            mem_mock = MagicMock()
            mem_mock.add.return_value = {"results": [{"id": "fact-1"}]}
            monkeypatch.setattr(
                "services.mem0.ingest._get_mem0", lambda: mem_mock,
            )
            monkeypatch.setattr(
                "services.mem0.ingest._refresh_mem0_client_token", lambda: None,
            )
            monkeypatch.setattr(
                "services.mem0.ingest.ensure_valid_token", AsyncMock(),
            )

            with (
                patch("services.mem0.ingest.extract_full_signals",
                      new_callable=AsyncMock,
                      return_value={"facts": facts, "relations": relations}),
                patch("config.MEM0_GATE_ENABLED", True),
                patch("config.MEM0_GATE_MIN_IMPORTANCE", 2),
                patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
                patch("config.MEM0_RELATION_EXTRACT_ENABLED", True),
            ):
                from services.mem0.ingest import add_memories
                await add_memories(
                    user_text="先輩はいぶきが好き", assistant_text="へえ",
                    user_id=USER_ID,
                )

            rels = graph_mod.get_relations(src, direction="src")
            assert len(rels) == 1
            assert rels[0].rel_type == "loves"
            assert rels[0].dst_entity_id == dst
        finally:
            os.unlink(tmp.name)

    @pytest.mark.asyncio
    async def test_relations_skipped_when_endpoint_missing(self, monkeypatch):
        from services.mem0 import graph as graph_mod
        import tempfile
        import os
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.close()
        monkeypatch.setattr(graph_mod, "_db_path", tmp.name)
        try:
            src = graph_mod.upsert_entity("haroin", "person", USER_ID)
            # NOTE: not creating the 砺波いぶき entity intentionally.

            facts = [{"speaker": "self", "text": "test", "importance": 3,
                      "category": "hobby", "subcategory": "", "tags": []}]
            relations = [{"src": "haroin", "rel_type": "loves", "dst": "砺波いぶき"}]

            mem_mock = MagicMock()
            mem_mock.add.return_value = {"results": [{"id": "fact-1"}]}
            monkeypatch.setattr(
                "services.mem0.ingest._get_mem0", lambda: mem_mock,
            )
            monkeypatch.setattr(
                "services.mem0.ingest._refresh_mem0_client_token", lambda: None,
            )
            monkeypatch.setattr(
                "services.mem0.ingest.ensure_valid_token", AsyncMock(),
            )

            with (
                patch("services.mem0.ingest.extract_full_signals",
                      new_callable=AsyncMock,
                      return_value={"facts": facts, "relations": relations}),
                patch("config.MEM0_GATE_ENABLED", True),
                patch("config.MEM0_GATE_MIN_IMPORTANCE", 2),
                patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
                patch("config.MEM0_RELATION_EXTRACT_ENABLED", True),
            ):
                from services.mem0.ingest import add_memories
                await add_memories(
                    user_text="x", assistant_text="y", user_id=USER_ID,
                )

            # No matching dst entity → relation must NOT be persisted.
            rels = graph_mod.get_relations(src, direction="src")
            assert rels == []
        finally:
            os.unlink(tmp.name)

    @pytest.mark.asyncio
    async def test_relations_skipped_when_disabled(self, monkeypatch):
        from services.mem0 import graph as graph_mod
        import tempfile
        import os
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.close()
        monkeypatch.setattr(graph_mod, "_db_path", tmp.name)
        try:
            src = graph_mod.upsert_entity("haroin", "person", USER_ID)
            dst = graph_mod.upsert_entity("砺波いぶき", "person", USER_ID)

            facts = [{"speaker": "self", "text": "x", "importance": 3,
                      "category": "hobby", "subcategory": "", "tags": []}]
            relations = [{"src": "haroin", "rel_type": "loves", "dst": "砺波いぶき"}]

            mem_mock = MagicMock()
            mem_mock.add.return_value = {"results": [{"id": "fact-1"}]}
            monkeypatch.setattr(
                "services.mem0.ingest._get_mem0", lambda: mem_mock,
            )
            monkeypatch.setattr(
                "services.mem0.ingest._refresh_mem0_client_token", lambda: None,
            )
            monkeypatch.setattr(
                "services.mem0.ingest.ensure_valid_token", AsyncMock(),
            )

            with (
                patch("services.mem0.ingest.extract_full_signals",
                      new_callable=AsyncMock,
                      return_value={"facts": facts, "relations": relations}),
                patch("config.MEM0_GATE_ENABLED", True),
                patch("config.MEM0_GATE_MIN_IMPORTANCE", 2),
                patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
                patch("config.MEM0_RELATION_EXTRACT_ENABLED", False),
            ):
                from services.mem0.ingest import add_memories
                await add_memories(
                    user_text="x", assistant_text="y", user_id=USER_ID,
                )

            rels = graph_mod.get_relations(src, direction="src")
            assert rels == []
        finally:
            os.unlink(tmp.name)

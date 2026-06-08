"""Unit tests for services/mem0/search.py — all external deps mocked."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module-level patches so imports inside search.py don't fail
# ---------------------------------------------------------------------------

_PATCH_BASE = "services.mem0.search"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_memory(mid: str, score: float, text: str = "some memory") -> dict:
    return {"id": mid, "memory": text, "score": score}


def _make_anthropic_resp(json_text: str):
    """Build a minimal fake anthropic messages response."""
    content_block = MagicMock()
    content_block.text = json_text
    resp = MagicMock()
    resp.content = [content_block]
    return resp


# ---------------------------------------------------------------------------
# search_memories
# ---------------------------------------------------------------------------

class TestSearchMemories:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_client(self, monkeypatch):
        monkeypatch.setattr(f"{_PATCH_BASE}._get_mem0", lambda: None)
        from services.mem0.search import search_memories
        result = await search_memories("query", "user1")
        assert result == []

    @pytest.mark.asyncio
    async def test_plain_search_returns_filtered_results(self, monkeypatch):
        # score is *distance* (small = similar). max_distance=1.0 keeps 0.1
        # and 0.9, drops 1.5 (too far to be relevant).
        mem_client = MagicMock()
        mem_client.search.return_value = {
            "results": [
                _make_memory("1", 0.1),
                _make_memory("2", 1.5),  # distance > MAX → filtered out
                _make_memory("3", 0.9),
            ]
        }
        monkeypatch.setattr(f"{_PATCH_BASE}._get_mem0", lambda: mem_client)
        monkeypatch.setattr(f"{_PATCH_BASE}.ensure_valid_token", AsyncMock())
        monkeypatch.setattr(f"{_PATCH_BASE}._refresh_mem0_client_token", lambda: None)
        monkeypatch.setattr(f"{_PATCH_BASE}.MEM0_RELEVANCE_MAX_DISTANCE", 1.0)

        from services.mem0.search import search_memories
        result = await search_memories("query", "user1")
        assert len(result) == 2
        assert all(r["score"] <= 1.0 for r in result)

    @pytest.mark.asyncio
    async def test_passes_limit_to_mem_search(self, monkeypatch):
        mem_client = MagicMock()
        mem_client.search.return_value = {"results": []}
        monkeypatch.setattr(f"{_PATCH_BASE}._get_mem0", lambda: mem_client)
        monkeypatch.setattr(f"{_PATCH_BASE}.ensure_valid_token", AsyncMock())
        monkeypatch.setattr(f"{_PATCH_BASE}._refresh_mem0_client_token", lambda: None)

        from services.mem0.search import search_memories
        await search_memories("q", "u", limit=3)
        call_kwargs = mem_client.search.call_args
        assert call_kwargs.kwargs.get("limit") == 3 or call_kwargs.args[2:] == (3,)

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, monkeypatch):
        mem_client = MagicMock()
        mem_client.search.side_effect = RuntimeError("boom")
        monkeypatch.setattr(f"{_PATCH_BASE}._get_mem0", lambda: mem_client)
        monkeypatch.setattr(f"{_PATCH_BASE}.ensure_valid_token", AsyncMock())
        monkeypatch.setattr(f"{_PATCH_BASE}._refresh_mem0_client_token", lambda: None)

        from services.mem0.search import search_memories
        result = await search_memories("q", "u")
        assert result == []

    @pytest.mark.asyncio
    async def test_accepts_list_response(self, monkeypatch):
        mem_client = MagicMock()
        # distance 0.8 ≤ MAX=1.0 → kept
        raw = [_make_memory("a", 0.8)]
        mem_client.search.return_value = raw
        monkeypatch.setattr(f"{_PATCH_BASE}._get_mem0", lambda: mem_client)
        monkeypatch.setattr(f"{_PATCH_BASE}.ensure_valid_token", AsyncMock())
        monkeypatch.setattr(f"{_PATCH_BASE}._refresh_mem0_client_token", lambda: None)
        monkeypatch.setattr(f"{_PATCH_BASE}.MEM0_RELEVANCE_MAX_DISTANCE", 1.0)

        from services.mem0.search import search_memories
        result = await search_memories("q", "u")
        assert result == raw

    @pytest.mark.asyncio
    async def test_no_score_field_passes_through(self, monkeypatch):
        """Memories without 'score' key should not be filtered out."""
        mem_client = MagicMock()
        mem_client.search.return_value = {"results": [{"id": "x", "memory": "hello"}]}
        monkeypatch.setattr(f"{_PATCH_BASE}._get_mem0", lambda: mem_client)
        monkeypatch.setattr(f"{_PATCH_BASE}.ensure_valid_token", AsyncMock())
        monkeypatch.setattr(f"{_PATCH_BASE}._refresh_mem0_client_token", lambda: None)

        from services.mem0.search import search_memories
        result = await search_memories("q", "u")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# search_memories_multi
# ---------------------------------------------------------------------------

class TestSearchMemoriesMulti:
    @pytest.mark.asyncio
    async def test_empty_queries_returns_empty(self):
        from services.mem0.search import search_memories_multi
        result = await search_memories_multi([], "u")
        assert result == []

    @pytest.mark.asyncio
    async def test_deduplicates_by_id(self, monkeypatch):
        shared = _make_memory("dup", 0.7)
        unique = _make_memory("uniq", 0.5)
        call_count = 0

        async def fake_search(q, uid, limit=8):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [shared, unique]
            return [shared]  # duplicate

        monkeypatch.setattr(f"{_PATCH_BASE}.search_memories", fake_search)

        from services.mem0.search import search_memories_multi
        result = await search_memories_multi(["q1", "q2"], "u")
        ids = [r["id"] for r in result]
        assert ids.count("dup") == 1
        assert "uniq" in ids

    @pytest.mark.asyncio
    async def test_sorted_by_score_asc(self, monkeypatch):
        """score is distance (small = similar), so merged order is ascending."""
        async def fake_search(q, uid, limit=8):
            return [_make_memory("a", 0.4), _make_memory("b", 0.9), _make_memory("c", 0.6)]

        monkeypatch.setattr(f"{_PATCH_BASE}.search_memories", fake_search)

        from services.mem0.search import search_memories_multi
        result = await search_memories_multi(["q"], "u")
        scores = [r["score"] for r in result]
        assert scores == sorted(scores)

    @pytest.mark.asyncio
    async def test_exception_in_one_query_is_skipped(self, monkeypatch):
        call_count = 0

        async def fake_search(q, uid, limit=8):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("fail")
            return [_make_memory("ok", 0.8)]

        monkeypatch.setattr(f"{_PATCH_BASE}.search_memories", fake_search)

        from services.mem0.search import search_memories_multi
        result = await search_memories_multi(["q1", "q2"], "u")
        assert len(result) == 1
        assert result[0]["id"] == "ok"

    @pytest.mark.asyncio
    async def test_respects_limit(self, monkeypatch):
        async def fake_search(q, uid, limit=8):
            return [_make_memory(str(i), float(i) / 10) for i in range(10)]

        monkeypatch.setattr(f"{_PATCH_BASE}.search_memories", fake_search)

        from services.mem0.search import search_memories_multi
        result = await search_memories_multi(["q"], "u", limit=3)
        assert len(result) <= 3


# ---------------------------------------------------------------------------
# _rewrite_query
# ---------------------------------------------------------------------------

class TestRewriteQuery:
    @pytest.mark.asyncio
    async def test_returns_queries_from_llm(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp('{"queries":["a","b","c"]}')
        )
        mock_get = AsyncMock(return_value=fake_client)
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", mock_get)

        from services.mem0 import search as search_mod
        # patch inside the module's closure
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            result = await search_mod._rewrite_query("test query")
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_anthropic_client(self, monkeypatch):
        mock_get = AsyncMock(return_value=None)
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            from services.mem0.search import _rewrite_query
            result = await _rewrite_query("test")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, monkeypatch):
        mock_get = AsyncMock(side_effect=RuntimeError("fail"))
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            from services.mem0.search import _rewrite_query
            result = await _rewrite_query("test")
        assert result == []

    @pytest.mark.asyncio
    async def test_strips_code_fences(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp('```json\n{"queries":["x","y"]}\n```')
        )
        mock_get = AsyncMock(return_value=fake_client)
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            from services.mem0.search import _rewrite_query
            result = await _rewrite_query("q")
        assert "x" in result
        assert "y" in result


# ---------------------------------------------------------------------------
# _rerank_memories
# ---------------------------------------------------------------------------

class TestRerankMemories:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        from services.mem0.search import _rerank_memories
        result = await _rerank_memories("q", [])
        assert result == []

    @pytest.mark.asyncio
    async def test_short_list_returned_as_is(self):
        mems = [_make_memory("a", 0.9), _make_memory("b", 0.8)]
        from services.mem0.search import _rerank_memories
        result = await _rerank_memories("q", mems, top_k=5)
        assert result == mems

    @pytest.mark.asyncio
    async def test_reranks_by_llm_order(self, monkeypatch):
        mems = [_make_memory(str(i), float(i) / 10) for i in range(6)]
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp('{"top":[3,1,5]}')
        )
        mock_get = AsyncMock(return_value=fake_client)
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            from services.mem0.search import _rerank_memories
            result = await _rerank_memories("q", mems, top_k=3)
        assert result[0] == mems[2]  # 1-indexed 3 → index 2
        assert result[1] == mems[0]  # 1-indexed 1 → index 0

    @pytest.mark.asyncio
    async def test_fallback_fill_when_reranker_returns_fewer(self, monkeypatch):
        mems = [_make_memory(str(i), float(i) / 10) for i in range(6)]
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp('{"top":[1]}')  # only 1 returned, top_k=3
        )
        mock_get = AsyncMock(return_value=fake_client)
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            from services.mem0.search import _rerank_memories
            result = await _rerank_memories("q", mems, top_k=3)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_fallback_on_no_anthropic_client(self, monkeypatch):
        mems = [_make_memory(str(i), 0.5) for i in range(6)]
        mock_get = AsyncMock(return_value=None)
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            from services.mem0.search import _rerank_memories
            result = await _rerank_memories("q", mems, top_k=3)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_returns_top_k_on_exception(self, monkeypatch):
        mems = [_make_memory(str(i), 0.5) for i in range(6)]
        mock_get = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            from services.mem0.search import _rerank_memories
            result = await _rerank_memories("q", mems, top_k=3)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_strips_code_fences_in_rerank(self, monkeypatch):
        mems = [_make_memory(str(i), 0.5) for i in range(6)]
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp('```json\n{"top":[2,3,4]}\n```')
        )
        mock_get = AsyncMock(return_value=fake_client)
        with patch("services.mem0.client.get_anthropic_client", mock_get):
            from services.mem0.search import _rerank_memories
            result = await _rerank_memories("q", mems, top_k=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# search_memories_smart
# ---------------------------------------------------------------------------

class TestSearchMemoriesSmart:
    @pytest.fixture(autouse=True)
    def _stub_alias_expand(self, monkeypatch):
        """Alias expansion would touch the real sqlite graph DB; stub it
        out by default so smart-pipeline tests stay deterministic."""
        monkeypatch.setattr(
            f"{_PATCH_BASE}._expand_query_via_graph",
            AsyncMock(return_value=[]),
        )

    @pytest.fixture(autouse=True)
    def _stub_hyde(self, monkeypatch):
        """HyDE talks to Anthropic; stub to empty string by default so
        smart-pipeline tests that focus on other behavior stay quiet."""
        monkeypatch.setattr(
            f"{_PATCH_BASE}._generate_hypothetical_fact",
            AsyncMock(return_value=""),
        )

    @pytest.fixture(autouse=True)
    def _stub_llm_keywords(self, monkeypatch):
        """LLM keyword extraction also talks to Anthropic; stub to '' by
        default so smart-pipeline tests stay deterministic."""
        monkeypatch.setattr(
            f"{_PATCH_BASE}._extract_keywords_via_llm",
            AsyncMock(return_value=""),
        )

    @pytest.mark.asyncio
    async def test_full_pipeline(self, monkeypatch):
        variants = ["kw1", "kw2"]
        merged = [_make_memory(str(i), float(i) / 10) for i in range(5)]
        reranked = merged[:3]

        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=variants))
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",AsyncMock(return_value=merged))
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=reranked))

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart("q", "u", limit=3)
        assert result == reranked

    @pytest.mark.asyncio
    async def test_alias_variants_appended_to_queries(self, monkeypatch):
        """When graph alias expansion fires, the canonical/alias forms are
        passed to search_memories_multi alongside the original query."""
        monkeypatch.setattr(
            f"{_PATCH_BASE}._expand_query_via_graph",
            AsyncMock(return_value=["CLANNAD", "Clannad"]),
        )
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=[]))
        multi_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",multi_mock)
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=[]))

        from services.mem0.search import search_memories_smart
        await search_memories_smart("クラナド", "u")
        passed = multi_mock.call_args.args[0]
        assert passed[0] == "クラナド"
        assert "CLANNAD" in passed
        assert "Clannad" in passed

    @pytest.mark.asyncio
    async def test_alias_expansion_runs_even_with_rewrite_disabled(self, monkeypatch):
        """Alias expansion is a sqlite lookup — should not depend on
        LLM rewrite being enabled."""
        alias_mock = AsyncMock(return_value=["CLANNAD"])
        monkeypatch.setattr(f"{_PATCH_BASE}._expand_query_via_graph", alias_mock)
        multi_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",multi_mock)
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=["unused"]))
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=[]))

        from services.mem0.search import search_memories_smart
        await search_memories_smart("クラナド", "u", rewrite=False)
        alias_mock.assert_awaited_once()
        passed = multi_mock.call_args.args[0]
        assert "CLANNAD" in passed
        # rewrite was disabled so the haiku-derived "unused" must NOT appear
        assert "unused" not in passed

    @pytest.mark.asyncio
    async def test_rewrite_disabled(self, monkeypatch):
        multi_mock = AsyncMock(return_value=[])
        rewrite_mock = AsyncMock(return_value=["x"])
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", rewrite_mock)
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",multi_mock)
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=[]))

        from services.mem0.search import search_memories_smart
        await search_memories_smart("q", "u", rewrite=False)
        rewrite_mock.assert_not_called()
        # multi called with only the original query
        call_args = multi_mock.call_args
        assert call_args.args[0] == ["q"]

    @pytest.mark.asyncio
    async def test_rerank_disabled(self, monkeypatch):
        mems = [_make_memory(str(i), 0.5) for i in range(10)]
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=[]))
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",AsyncMock(return_value=mems))
        rerank_mock = AsyncMock(return_value=mems[:5])
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", rerank_mock)

        from services.mem0.search import search_memories_smart
        await search_memories_smart("q", "u", limit=5, rerank=False)
        rerank_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_deduplicates_original_from_variants(self, monkeypatch):
        """If rewrite returns the same query, it should not be duplicated."""
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=["q", "other"]))
        multi_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",multi_mock)
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=[]))

        from services.mem0.search import search_memories_smart
        await search_memories_smart("q", "u")
        queries_passed = multi_mock.call_args.args[0]
        assert queries_passed.count("q") == 1

    @pytest.mark.asyncio
    async def test_alias_expansion_failure_is_non_fatal(self, monkeypatch):
        """If the graph DB is unavailable, smart search must still proceed
        with just the original (and rewrite) queries."""
        from services.mem0.search import _expand_query_via_graph
        # Force the underlying find_entity to blow up — _expand_query_via_graph
        # should swallow it and return [].
        with patch("services.mem0.graph.find_entity", side_effect=RuntimeError("disk full")):
            out = await _expand_query_via_graph("anything", "u")
        assert out == []

    @pytest.mark.asyncio
    async def test_both_disabled_plain_search(self, monkeypatch):
        # ADR-0002: rerank=False path uses MEM0_RELEVANCE_MAX_DISTANCE
        # (1.2) as the sanity gate. 0.4 is well under either threshold.
        mems = [_make_memory("x", 0.4)]
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",AsyncMock(return_value=mems))
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock())
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock())

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart("q", "u", limit=5, rewrite=False, rerank=False)
        assert result == mems


# ---------------------------------------------------------------------------
# HyDE (Phase B-1)
# ---------------------------------------------------------------------------

class TestHydeGeneration:
    @pytest.mark.asyncio
    async def test_returns_empty_on_short_query(self):
        from services.mem0.search import _generate_hypothetical_fact
        assert await _generate_hypothetical_fact("") == ""
        assert await _generate_hypothetical_fact("       ") == ""
        assert await _generate_hypothetical_fact("short") == ""  # <8 chars

    @pytest.mark.asyncio
    async def test_returns_empty_when_disabled(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MEM0_HYDE_ENABLED", False, raising=False)
        from services.mem0.search import _generate_hypothetical_fact
        assert await _generate_hypothetical_fact("いぶきの髪色を覚えてる？") == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_client_none(self, monkeypatch):
        async def _no_client():
            return None
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", _no_client)
        from services.mem0.search import _generate_hypothetical_fact
        assert await _generate_hypothetical_fact("先輩はいぶきが好き?") == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_json_error(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp("not json at all"),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.search import _generate_hypothetical_fact
        result = await _generate_hypothetical_fact("先輩はいぶきが好き?")
        assert result == ""

    @pytest.mark.asyncio
    async def test_extracts_hypothetical_from_json(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"hypothetical":"先輩は砺波いぶきが好き"}',
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.search import _generate_hypothetical_fact
        result = await _generate_hypothetical_fact("先輩はいぶきが好き?")
        assert result == "先輩は砺波いぶきが好き"

    @pytest.mark.asyncio
    async def test_strips_code_fence(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '```json\n{"hypothetical":"先輩はRustが好き"}\n```',
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.search import _generate_hypothetical_fact
        result = await _generate_hypothetical_fact("先輩はRustの話してた?")
        assert result == "先輩はRustが好き"


class TestSmartSearchHydeIntegration:
    @pytest.fixture(autouse=True)
    def _stub_alias_expand(self, monkeypatch):
        monkeypatch.setattr(
            f"{_PATCH_BASE}._expand_query_via_graph",
            AsyncMock(return_value=[]),
        )

    @pytest.fixture(autouse=True)
    def _stub_llm_keywords(self, monkeypatch):
        monkeypatch.setattr(
            f"{_PATCH_BASE}._extract_keywords_via_llm",
            AsyncMock(return_value=""),
        )

    @pytest.mark.asyncio
    async def test_hyde_string_passed_to_multi_search(self, monkeypatch):
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._generate_hypothetical_fact",
            AsyncMock(return_value="先輩は砺波いぶきが好き"),
        )
        multi_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid", multi_mock)
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=[]),
        )
        from services.mem0.search import search_memories_smart
        await search_memories_smart("先輩はいぶきが好き?", "u")
        queries = multi_mock.call_args.args[0]
        assert "先輩は砺波いぶきが好き" in queries

    @pytest.mark.asyncio
    async def test_hyde_not_called_when_rewrite_disabled(self, monkeypatch):
        rewrite_mock = AsyncMock(return_value=["unused"])
        hyde_mock = AsyncMock(return_value="should_not_be_called")
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", rewrite_mock)
        monkeypatch.setattr(f"{_PATCH_BASE}._generate_hypothetical_fact", hyde_mock)
        monkeypatch.setattr(
            f"{_PATCH_BASE}._search_multi_hybrid", AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=[]),
        )
        from services.mem0.search import search_memories_smart
        await search_memories_smart("先輩はいぶきが好き?", "u", rewrite=False)
        rewrite_mock.assert_not_called()
        hyde_mock.assert_not_called()


class TestLlmKeywordExtraction:
    @pytest.mark.asyncio
    async def test_returns_empty_on_short_query(self):
        from services.mem0.search import _extract_keywords_via_llm
        assert await _extract_keywords_via_llm("") == ""
        assert await _extract_keywords_via_llm("short") == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_client_none(self, monkeypatch):
        async def _no_client():
            return None
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client", _no_client,
        )
        from services.mem0.search import _extract_keywords_via_llm
        assert await _extract_keywords_via_llm("先輩は砺波いぶきが好きですか？") == ""

    @pytest.mark.asyncio
    async def test_extracts_keywords_and_aliases(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"keywords":["先輩","砺波いぶき","髪色"],'
                '"aliases":["haroin"]}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.search import _extract_keywords_via_llm
        result = await _extract_keywords_via_llm("先輩はいぶきの髪色覚えてる？")
        parts = result.split()
        assert "砺波いぶき" in parts
        assert "haroin" in parts

    @pytest.mark.asyncio
    async def test_dedupes_keyword_and_alias_overlap(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"keywords":["砺波いぶき"],"aliases":["砺波いぶき","いぶき"]}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.search import _extract_keywords_via_llm
        result = await _extract_keywords_via_llm("砺波いぶきの話覚えてる？")
        parts = result.split()
        assert parts.count("砺波いぶき") == 1
        assert "いぶき" in parts

    @pytest.mark.asyncio
    async def test_fail_open_on_invalid_json(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp("not json"),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.search import _extract_keywords_via_llm
        assert await _extract_keywords_via_llm("先輩はいぶきが好きですか？") == ""


class TestEpisodeBundle:
    """Phase C-3: smart_search expands top-1 hit's episode_id into a
    bundle of sibling facts."""

    @pytest.fixture(autouse=True)
    def _stub_pipeline(self, monkeypatch):
        monkeypatch.setattr(
            f"{_PATCH_BASE}._expand_query_via_graph",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._generate_hypothetical_fact",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=[]),
        )

    @pytest.mark.asyncio
    async def test_top_with_episode_pulls_siblings(self, monkeypatch):
        top_hit = {
            "id": "f1", "memory": "topic A fact",
            "score": 0.1, "metadata": {"episode_id": "ep-42"},
        }
        sibling = {
            "id": "f2", "memory": "more about A",
            "score": None, "metadata": {"episode_id": "ep-42"},
        }
        monkeypatch.setattr(
            f"{_PATCH_BASE}._search_multi_hybrid",
            AsyncMock(return_value=[top_hit]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rerank_memories",
            AsyncMock(return_value=[top_hit]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}.get_episode_facts",
            AsyncMock(return_value=[sibling]),
        )

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart("q", "u", limit=5)
        ids = [r["id"] for r in result]
        assert "f1" in ids
        assert "f2" in ids

    @pytest.mark.asyncio
    async def test_no_episode_skips_bundle(self, monkeypatch):
        top_hit = {"id": "f1", "memory": "no episode", "score": 0.1, "metadata": {}}
        monkeypatch.setattr(
            f"{_PATCH_BASE}._search_multi_hybrid",
            AsyncMock(return_value=[top_hit]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rerank_memories",
            AsyncMock(return_value=[top_hit]),
        )
        bundle_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(f"{_PATCH_BASE}.get_episode_facts", bundle_mock)

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart("q", "u", limit=5)
        bundle_mock.assert_not_called()
        assert [r["id"] for r in result] == ["f1"]

    @pytest.mark.asyncio
    async def test_bundle_caps_at_limit(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MEM0_EPISODE_BUNDLE_LIMIT", 2, raising=False)

        top_hit = {
            "id": "f1", "memory": "topic", "score": 0.1,
            "metadata": {"episode_id": "ep-1"},
        }
        siblings = [
            {"id": f"f{i}", "memory": "sib", "metadata": {"episode_id": "ep-1"}}
            for i in range(2, 10)
        ]
        monkeypatch.setattr(
            f"{_PATCH_BASE}._search_multi_hybrid",
            AsyncMock(return_value=[top_hit]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rerank_memories",
            AsyncMock(return_value=[top_hit]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}.get_episode_facts",
            AsyncMock(return_value=siblings),
        )

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart("q", "u", limit=5)
        # top-1 + cap (2) = 3 entries
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_bundle_disabled_via_flag(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MEM0_EPISODE_BUNDLE_ENABLED", False, raising=False)

        top_hit = {
            "id": "f1", "memory": "topic", "score": 0.1,
            "metadata": {"episode_id": "ep-1"},
        }
        monkeypatch.setattr(
            f"{_PATCH_BASE}._search_multi_hybrid",
            AsyncMock(return_value=[top_hit]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rerank_memories",
            AsyncMock(return_value=[top_hit]),
        )
        bundle_mock = AsyncMock(return_value=[{"id": "f2"}])
        monkeypatch.setattr(f"{_PATCH_BASE}.get_episode_facts", bundle_mock)

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart("q", "u", limit=5)
        bundle_mock.assert_not_called()
        assert [r["id"] for r in result] == ["f1"]


class TestGetEpisodeFacts:
    @pytest.mark.asyncio
    async def test_uses_filters_kwarg_when_supported(self, monkeypatch):
        mem = MagicMock()
        mem.get_all.return_value = {
            "results": [
                {"id": "f1", "memory": "x", "metadata": {"episode_id": "ep-1"}},
            ],
        }
        monkeypatch.setattr(f"{_PATCH_BASE}._get_mem0", lambda: mem)
        from services.mem0.search import get_episode_facts
        out = await get_episode_facts("ep-1", "u")
        assert [r["id"] for r in out] == ["f1"]

    @pytest.mark.asyncio
    async def test_falls_back_when_filters_unsupported(self, monkeypatch):
        mem = MagicMock()
        # First call (with filters kwarg) raises TypeError → fallback path.
        first_call = {"counter": 0}

        def _get_all(*args, **kwargs):
            first_call["counter"] += 1
            if "filters" in kwargs:
                raise TypeError("unexpected kwarg 'filters'")
            return {"results": [
                {"id": "f1", "memory": "x",
                 "metadata": {"episode_id": "ep-1"}},
                {"id": "f2", "memory": "y",
                 "metadata": {"episode_id": "ep-OTHER"}},
            ]}
        mem.get_all = _get_all
        monkeypatch.setattr(f"{_PATCH_BASE}._get_mem0", lambda: mem)
        from services.mem0.search import get_episode_facts
        out = await get_episode_facts("ep-1", "u")
        # Python-side filter discards ep-OTHER.
        assert [r["id"] for r in out] == ["f1"]


class TestMem0MetricsLog:
    """Phase B-3: smart_search must emit one [MEM0_METRICS ...] line per call."""

    @pytest.fixture(autouse=True)
    def _stub_alias_and_hyde(self, monkeypatch):
        monkeypatch.setattr(
            f"{_PATCH_BASE}._expand_query_via_graph",
            AsyncMock(return_value=[]),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._generate_hypothetical_fact",
            AsyncMock(return_value=""),
        )

    @pytest.mark.asyncio
    async def test_metric_emitted_on_rerank_path(self, monkeypatch, caplog):
        import logging as _logging
        caplog.set_level(_logging.INFO, logger="services.mem0.search")

        mems = [_make_memory(str(i), 0.3) for i in range(5)]
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            f"{_PATCH_BASE}._search_multi_hybrid",
            AsyncMock(return_value=mems),
        )
        monkeypatch.setattr(
            f"{_PATCH_BASE}._rerank_memories",
            AsyncMock(return_value=mems[:3]),
        )
        from services.mem0.search import search_memories_smart
        await search_memories_smart("query string", "u", limit=3)
        joined = "\n".join(r.message for r in caplog.records)
        assert "MEM0_METRICS" in joined
        assert "kept=3" in joined
        assert "rerank=on" in joined

    @pytest.mark.asyncio
    async def test_metric_emitted_on_plain_path(self, monkeypatch, caplog):
        import logging as _logging
        caplog.set_level(_logging.INFO, logger="services.mem0.search")

        mems = [_make_memory(str(i), 0.3) for i in range(5)]
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            f"{_PATCH_BASE}._search_multi_hybrid",
            AsyncMock(return_value=mems),
        )
        from services.mem0.search import search_memories_smart
        await search_memories_smart("query string", "u", limit=3, rerank=False)
        joined = "\n".join(r.message for r in caplog.records)
        assert "MEM0_METRICS" in joined
        assert "rerank=off" in joined


class TestPostRerankGateRedesign:
    """ADR-0002: post-rerank cosine gate is removed; LLM rerank is final.

    Regression coverage for incident 2026-05-19 (rid=10c036ee) where the
    legacy 0.65 gate silently dropped every Japanese conversational query.
    """

    @pytest.mark.asyncio
    async def test_rerank_path_returns_high_distance_results(self, monkeypatch):
        # The incident replay: every candidate has score >> 0.65 because
        # cosine distance on a 30-char conversational query never lands
        # in that band. Rerank LLM accepted them, so smart search must
        # return them verbatim.
        candidates = [_make_memory("上伊那ぼたん 漫画", 0.92)]
        reranked = list(candidates)
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",AsyncMock(return_value=candidates))
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=[]))
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=reranked))

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart(
            "まあまあ、上伊那ぼたんのFAでも見て落ち着きなよ", "u",
            limit=5, rewrite=False, rerank=True,
        )
        assert result == reranked
        assert result[0]["score"] == 0.92

    @pytest.mark.asyncio
    async def test_rerank_path_passes_through_empty_rerank(self, monkeypatch):
        # If the LLM judges nothing relevant, return empty — not the raw
        # candidate set.
        candidates = [_make_memory("x", 0.4), _make_memory("y", 0.5)]
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",AsyncMock(return_value=candidates))
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock(return_value=[]))
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock(return_value=[]))

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart("q", "u", limit=5, rewrite=False, rerank=True)
        assert result == []

    @pytest.mark.asyncio
    async def test_plain_path_applies_sanity_gate_at_1_2(self, monkeypatch):
        # rerank=False has no LLM judge, so the sanity gate stays — but
        # at the loose MEM0_RELEVANCE_MAX_DISTANCE (1.2), not the legacy
        # 0.65. Score 0.9 passes here; score 1.3 does not.
        mems = [
            _make_memory("kept-id", 0.9, text="kept"),
            _make_memory("dropped-id", 1.3, text="dropped"),
        ]
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid",AsyncMock(return_value=mems))
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", AsyncMock())
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", AsyncMock())

        from services.mem0.search import search_memories_smart
        result = await search_memories_smart("q", "u", limit=5, rewrite=False, rerank=False)
        assert [r.get("memory") for r in result] == ["kept"]


# ---------------------------------------------------------------------------
# M2 security fixes — appended from worktree-security-remediation
# ---------------------------------------------------------------------------

import asyncio
import unittest.mock as mock


def test_rerank_candidates_wrapped_as_untrusted():
    """M2: _rerank_memories must wrap each candidate in <untrusted_candidate> tags."""
    from services.mem0.search import _rerank_memories

    memories = [
        {"memory": "先輩はTOEICの勉強をしている", "score": 0.9},
        {"memory": "先輩はRustが好き", "score": 0.8},
        {"memory": "先輩はアシタボの開発者", "score": 0.7},
        {"memory": "先輩は長野市在住", "score": 0.6},
        {"memory": "先輩はclannad好き", "score": 0.5},
        {"memory": "先輩はVPS運用中", "score": 0.4},
    ]

    captured_prompt: list[str] = []

    fake_response = mock.MagicMock()
    fake_response.content = [mock.MagicMock(text='{"top":[1,2,3]}')]

    async def fake_create(**kwargs):
        msg_content = kwargs["messages"][0]["content"]
        captured_prompt.append(msg_content)
        return fake_response

    async def run():
        with mock.patch("services.mem0.client.get_anthropic_client") as mock_get_client:
            fake_client = mock.AsyncMock()
            fake_client.messages.create = fake_create
            mock_get_client.return_value = fake_client
            return await _rerank_memories("TOEIC", memories, top_k=3)

    result = asyncio.run(run())

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    assert "<untrusted_candidate>" in prompt
    assert "</untrusted_candidate>" in prompt


def test_rewrite_query_length_cap():
    """M2: _rewrite_query must cap query at 500 chars before format()."""
    from services.mem0.search import _rewrite_query

    long_query = "x" * 600

    fake_response = mock.MagicMock()
    fake_response.content = [mock.MagicMock(text='{"queries":["q1","q2"]}')]

    captured_prompt: list[str] = []

    async def fake_create(**kwargs):
        msg_content = kwargs["messages"][0]["content"]
        captured_prompt.append(msg_content)
        return fake_response

    async def run():
        with mock.patch("services.mem0.client.get_anthropic_client") as mock_get_client:
            fake_client = mock.AsyncMock()
            fake_client.messages.create = fake_create
            mock_get_client.return_value = fake_client
            return await _rewrite_query(long_query)

    result = asyncio.run(run())

    assert len(captured_prompt) == 1
    # The 600-char query should be capped to 500 in the prompt
    assert "x" * 501 not in captured_prompt[0]


def test_rerank_brace_injection_in_candidate():
    """M2: braces in candidate memory text must not cause KeyError in format()."""
    from services.mem0.search import _rerank_memories

    memories = [
        {"memory": "fact with {brace} injection", "score": 0.9},
        {"memory": "normal fact", "score": 0.8},
        {"memory": "another fact", "score": 0.7},
        {"memory": "yet another", "score": 0.6},
        {"memory": "fifth fact", "score": 0.5},
        {"memory": "sixth fact", "score": 0.4},
    ]

    fake_response = mock.MagicMock()
    fake_response.content = [mock.MagicMock(text='{"top":[1,2,3]}')]

    async def run():
        with mock.patch("services.mem0.client.get_anthropic_client") as mock_get_client:
            fake_client = mock.AsyncMock()
            fake_client.messages.create = mock.AsyncMock(return_value=fake_response)
            mock_get_client.return_value = fake_client
            # Must not raise KeyError
            return await _rerank_memories("{category}", memories, top_k=3)

    result = asyncio.run(run())
    assert isinstance(result, list)


class TestApplyReinforcementBoost:
    """Per-user recency/engagement ordering, now applied on the plain path too
    (not just the rerank=True recall-intent path)."""

    def test_high_mention_count_sorts_ahead(self):
        from services.mem0.search import _apply_reinforcement_boost
        mems = [
            {"id": "cold", "score": 0.50, "metadata": {"mention_count": 1}},
            {"id": "hot", "score": 0.50, "metadata": {"mention_count": 50}},
        ]
        _apply_reinforcement_boost(mems)
        # the frequently-reinforced fact gets its distance shrunk → sorts first
        assert mems[0]["id"] == "hot"
        assert mems[0]["score"] < 0.50

    def test_no_metadata_is_noop_and_safe(self):
        from services.mem0.search import _apply_reinforcement_boost
        mems = [{"id": "a", "score": 0.3}, {"id": "b", "score": 0.1}]
        _apply_reinforcement_boost(mems)  # must not raise
        # sorted ascending by distance regardless
        assert [m["id"] for m in mems] == ["b", "a"]

    def test_empty_list_safe(self):
        from services.mem0.search import _apply_reinforcement_boost
        empty = []
        _apply_reinforcement_boost(empty)
        assert empty == []

"""Tests for services/mem0/conflict.py (classify_conflict) and the
end-to-end ingest → archive flow for Phase B-2."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_anthropic_resp(json_text: str):
    content_block = MagicMock()
    content_block.text = json_text
    resp = MagicMock()
    resp.content = [content_block]
    return resp


class TestClassifyConflict:
    @pytest.mark.asyncio
    async def test_returns_update_when_haiku_says_update(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"conflict_type":"update","reason":"hair changed"}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.conflict import classify_conflict
        result = await classify_conflict("先輩の髪は黒", "先輩の髪はハイトーン")
        assert result["conflict_type"] == "update"
        assert result["reason"] == "hair changed"

    @pytest.mark.asyncio
    async def test_returns_addition_when_haiku_says_addition(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"conflict_type":"addition","reason":"diff aspect"}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.conflict import classify_conflict
        result = await classify_conflict("Rust が好き", "TypeScript が好き")
        assert result["conflict_type"] == "addition"

    @pytest.mark.asyncio
    async def test_fail_open_on_client_none(self, monkeypatch):
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=None),
        )
        from services.mem0.conflict import classify_conflict
        result = await classify_conflict("a", "b")
        assert result["conflict_type"] == "addition"
        assert "no-client" in result["reason"]

    @pytest.mark.asyncio
    async def test_fail_open_on_exception(self, monkeypatch):
        async def _boom():
            raise RuntimeError("boom")
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client", _boom,
        )
        from services.mem0.conflict import classify_conflict
        result = await classify_conflict("a", "b")
        assert result["conflict_type"] == "addition"
        assert result["reason"] == "fail-open"

    @pytest.mark.asyncio
    async def test_unknown_type_falls_back_to_addition(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '{"conflict_type":"explosion","reason":"???"}'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.conflict import classify_conflict
        result = await classify_conflict("a", "b")
        assert result["conflict_type"] == "addition"
        assert "unknown-type" in result["reason"]

    @pytest.mark.asyncio
    async def test_strips_code_fence(self, monkeypatch):
        fake_client = AsyncMock()
        fake_client.messages.create = AsyncMock(
            return_value=_make_anthropic_resp(
                '```json\n{"conflict_type":"correction","reason":"r"}\n```'
            ),
        )
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=fake_client),
        )
        from services.mem0.conflict import classify_conflict
        result = await classify_conflict("中野梓やる", "やっぱ澪")
        assert result["conflict_type"] == "correction"


class TestCheckNearDupDetectConflict:
    @pytest.mark.asyncio
    async def test_gray_non_same_with_update_returns_conflict_tuple(self, monkeypatch):
        """detect_conflict=True + gray-zone different + Haiku says 'update'
        → (False, 'conflict_update...', existing_id, 'update')."""
        from services.mem0 import dedup

        # Patch the LLM judge to say "not same"
        async def _judge(_e, _c):
            return False
        monkeypatch.setattr("services.mem0.dedup.llm_judge_same_fact", _judge)

        # Patch classify_conflict to return update
        async def _classify(_e, _c):
            return {"conflict_type": "update", "reason": "state change"}
        monkeypatch.setattr("services.mem0.conflict.classify_conflict", _classify)

        result = await dedup.check_near_dup(
            "先輩の髪はハイトーン",
            None,
            [{"id": "old1", "memory": "先輩の髪は黒", "score": 0.25}],
            detect_conflict=True,
        )
        is_dup, reason, existing_id, conflict_type = result
        assert is_dup is False
        assert "conflict_update" in reason
        assert existing_id == "old1"
        assert conflict_type == "update"

    @pytest.mark.asyncio
    async def test_gray_non_same_with_addition_returns_no_conflict(self, monkeypatch):
        """addition verdict shouldn't archive — return novel with no
        conflict tag."""
        from services.mem0 import dedup

        async def _judge(_e, _c):
            return False
        monkeypatch.setattr("services.mem0.dedup.llm_judge_same_fact", _judge)

        async def _classify(_e, _c):
            return {"conflict_type": "addition", "reason": "diff aspect"}
        monkeypatch.setattr("services.mem0.conflict.classify_conflict", _classify)

        result = await dedup.check_near_dup(
            "TypeScript が好き",
            None,
            [{"id": "old1", "memory": "Rust が好き", "score": 0.25}],
            detect_conflict=True,
        )
        is_dup, reason, existing_id, conflict_type = result
        assert is_dup is False
        assert reason == "novel"
        assert conflict_type is None

    @pytest.mark.asyncio
    async def test_detect_conflict_disabled_skips_classify(self, monkeypatch):
        """detect_conflict=False → classify_conflict must NOT be called."""
        from services.mem0 import dedup

        async def _judge(_e, _c):
            return False
        monkeypatch.setattr("services.mem0.dedup.llm_judge_same_fact", _judge)

        classify_mock = AsyncMock(return_value={"conflict_type": "update"})
        monkeypatch.setattr("services.mem0.conflict.classify_conflict", classify_mock)

        await dedup.check_near_dup(
            "candidate",
            None,
            [{"id": "x", "memory": "existing", "score": 0.25}],
            detect_conflict=False,
        )
        classify_mock.assert_not_called()


class TestSearchHidesArchived:
    @pytest.mark.asyncio
    async def test_valid_to_future_kept(self, monkeypatch):
        """valid_to in the future → fact is still valid, must be returned."""
        from services.mem0.search import search_memories
        future = 9_999_999_999
        mem_client = MagicMock()
        mem_client.search.return_value = {"results": [
            {"id": "f1", "memory": "alive", "score": 0.5,
             "metadata": {"valid_to": future}},
        ]}
        monkeypatch.setattr("services.mem0.search._get_mem0", lambda: mem_client)
        monkeypatch.setattr("services.mem0.search.ensure_valid_token", AsyncMock())
        monkeypatch.setattr("services.mem0.search._refresh_mem0_client_token", lambda: None)
        out = await search_memories("q", "u")
        assert [r["id"] for r in out] == ["f1"]

    @pytest.mark.asyncio
    async def test_valid_to_past_hidden(self, monkeypatch):
        """valid_to in the past → archived, hidden by default."""
        from services.mem0.search import search_memories
        mem_client = MagicMock()
        mem_client.search.return_value = {"results": [
            {"id": "f1", "memory": "stale", "score": 0.5,
             "metadata": {"valid_to": 1}},
        ]}
        monkeypatch.setattr("services.mem0.search._get_mem0", lambda: mem_client)
        monkeypatch.setattr("services.mem0.search.ensure_valid_token", AsyncMock())
        monkeypatch.setattr("services.mem0.search._refresh_mem0_client_token", lambda: None)
        out = await search_memories("q", "u")
        assert out == []

    @pytest.mark.asyncio
    async def test_no_valid_to_kept(self, monkeypatch):
        """Legacy facts without valid_to must always pass."""
        from services.mem0.search import search_memories
        mem_client = MagicMock()
        mem_client.search.return_value = {"results": [
            {"id": "f1", "memory": "legacy", "score": 0.5},
        ]}
        monkeypatch.setattr("services.mem0.search._get_mem0", lambda: mem_client)
        monkeypatch.setattr("services.mem0.search.ensure_valid_token", AsyncMock())
        monkeypatch.setattr("services.mem0.search._refresh_mem0_client_token", lambda: None)
        out = await search_memories("q", "u")
        assert [r["id"] for r in out] == ["f1"]

    @pytest.mark.asyncio
    async def test_hide_disable_flag_restores_archived(self, monkeypatch):
        """MEM0_HIDE_ARCHIVED=False → even archived facts are returned."""
        import config
        monkeypatch.setattr(config, "MEM0_HIDE_ARCHIVED", False, raising=False)
        from services.mem0.search import search_memories
        mem_client = MagicMock()
        mem_client.search.return_value = {"results": [
            {"id": "f1", "memory": "stale", "score": 0.5,
             "metadata": {"valid_to": 1}},
        ]}
        monkeypatch.setattr("services.mem0.search._get_mem0", lambda: mem_client)
        monkeypatch.setattr("services.mem0.search.ensure_valid_token", AsyncMock())
        monkeypatch.setattr("services.mem0.search._refresh_mem0_client_token", lambda: None)
        out = await search_memories("q", "u")
        assert [r["id"] for r in out] == ["f1"]


class TestArchiveOutdatedFacts:
    @pytest.mark.asyncio
    async def test_writes_valid_to_and_tag(self):
        from services.mem0.ingest import _archive_outdated_facts
        mem = MagicMock()
        mem.get.return_value = {"id": "old1", "memory": "先輩の髪は黒"}
        mem.update.return_value = None
        n = await _archive_outdated_facts(mem, [("old1", "update")])
        assert n == 1
        kwargs = mem.update.call_args.kwargs
        assert kwargs["memory_id"] == "old1"
        assert kwargs["data"] == "先輩の髪は黒"
        partial = kwargs["metadata"]
        assert "valid_to" in partial
        assert partial["conflict_archived_as"] == "update"

    @pytest.mark.asyncio
    async def test_failure_is_non_fatal(self):
        from services.mem0.ingest import _archive_outdated_facts
        mem = MagicMock()
        mem.get.side_effect = RuntimeError("boom")
        n = await _archive_outdated_facts(mem, [("old1", "update")])
        # Failure path: returns 0 archived, never raises.
        assert n == 0

    @pytest.mark.asyncio
    async def test_empty_archive_list(self):
        from services.mem0.ingest import _archive_outdated_facts
        mem = MagicMock()
        n = await _archive_outdated_facts(mem, [])
        assert n == 0

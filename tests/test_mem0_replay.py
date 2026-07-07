"""Tests for services.mem0.replay — Phase ③ replay + schema abstraction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.mem0 import replay


# ---------------------------------------------------------------------------
# (a) _pair_turns — pure logic
# ---------------------------------------------------------------------------
class TestPairTurns:
    def test_pairs_user_with_following_assistant(self):
        lines = [
            {"ts": "t1", "role": "user", "author": "", "text": "こんにちは"},
            {"ts": "t2", "role": "assistant", "author": "", "text": "やあ"},
        ]
        turns, last_ts = replay._pair_turns(lines)
        assert turns == [("こんにちは", "やあ")]
        assert last_ts == "t2"

    def test_user_without_assistant_reply(self):
        lines = [{"ts": "t1", "role": "user", "author": "", "text": "誰か見てる？"}]
        turns, last_ts = replay._pair_turns(lines)
        assert turns == [("誰か見てる？", "")]
        assert last_ts == "t1"

    def test_skips_channel_role_lines(self):
        lines = [
            {"ts": "t1", "role": "channel", "author": "b0nchichi", "text": "雑談"},
            {"ts": "t2", "role": "user", "author": "", "text": "本題"},
            {"ts": "t3", "role": "assistant", "author": "", "text": "了解"},
        ]
        turns, last_ts = replay._pair_turns(lines)
        assert turns == [("本題", "了解")]
        assert last_ts == "t3"

    def test_empty_input(self):
        assert replay._pair_turns([]) == ([], "")


# ---------------------------------------------------------------------------
# (a) replay_channel_history — integration with mocks
# ---------------------------------------------------------------------------
class TestReplayChannelHistory:
    @pytest.mark.asyncio
    async def test_dry_run_never_advances_cursor(self, monkeypatch, tmp_state_dir):
        lines = [
            {"ts": "t1", "role": "user", "author": "", "text": "Rust勉強してる"},
            {"ts": "t2", "role": "assistant", "author": "", "text": "いいね"},
        ]
        monkeypatch.setattr(
            "services.mem0.history_index.get_lines_since",
            lambda ch, since, limit: lines,
        )
        add_memories_mock = AsyncMock(return_value={"kept": 1, "results": []})
        monkeypatch.setattr("services.mem0.ingest.add_memories", add_memories_mock)

        summary = await replay.replay_channel_history(123, "u1", dry_run=True)

        assert summary["turns_processed"] == 1
        assert summary["facts_kept"] == 0  # dry-run never calls add_memories
        add_memories_mock.assert_not_called()
        assert not replay._cursor_path().exists()

    @pytest.mark.asyncio
    async def test_apply_advances_cursor_and_tags_source(self, monkeypatch, tmp_state_dir):
        lines = [
            {"ts": "t1", "role": "user", "author": "", "text": "Rust勉強してる"},
            {"ts": "t2", "role": "assistant", "author": "", "text": "いいね"},
        ]
        monkeypatch.setattr(
            "services.mem0.history_index.get_lines_since",
            lambda ch, since, limit: lines,
        )
        add_memories_mock = AsyncMock(return_value={"kept": 2, "results": []})
        monkeypatch.setattr("services.mem0.ingest.add_memories", add_memories_mock)

        summary = await replay.replay_channel_history(123, "u1", dry_run=False)

        assert summary["turns_processed"] == 1
        assert summary["facts_kept"] == 2
        assert summary["cursor_advanced_to"] == "t2"
        add_memories_mock.assert_called_once()
        kwargs = add_memories_mock.call_args.kwargs
        assert kwargs["metadata"]["source"] == "replay"
        assert kwargs["user_text"] == "Rust勉強してる"
        assert kwargs["assistant_text"] == "いいね"

        cursor = replay._load_cursor()
        assert cursor["123"] == "t2"

    @pytest.mark.asyncio
    async def test_second_run_starts_from_saved_cursor(self, monkeypatch, tmp_state_dir):
        seen_since = []

        def _fake_get_lines_since(ch, since, limit):
            seen_since.append(since)
            return []

        monkeypatch.setattr(
            "services.mem0.history_index.get_lines_since", _fake_get_lines_since,
        )
        monkeypatch.setattr(
            "services.mem0.ingest.add_memories",
            AsyncMock(return_value={"kept": 0, "results": []}),
        )

        replay._save_cursor({"123": "t5"})
        await replay.replay_channel_history(123, "u1", dry_run=False)

        assert seen_since == ["t5"]


# ---------------------------------------------------------------------------
# (b) abstract_schemas_for_user
# ---------------------------------------------------------------------------
def _entity(eid, etype, mention_count=5):
    e = MagicMock()
    e.id = eid
    e.type = etype
    e.mention_count = mention_count
    return e


class TestAbstractSchemasForUser:
    @pytest.mark.asyncio
    async def test_requires_distinct_entities(self, monkeypatch):
        rows = [
            {"id": "f1", "memory": "Blade Runnerが好き",
             "metadata": {"category": "hobby", "subcategory": "movie"}},
            {"id": "f2", "memory": "伊藤計劃が好き",
             "metadata": {"category": "hobby", "subcategory": "movie"}},
        ]  # only 2 distinct entities — below SCHEMA_MIN_DISTINCT_ENTITIES (3)
        monkeypatch.setattr(
            "services.mem0.client.get_all_memories", AsyncMock(return_value=rows),
        )

        def _fake_entities(fid):
            return [_entity({"f1": 1, "f2": 2}[fid], "media")]

        monkeypatch.setattr("services.mem0.graph.get_fact_entities", _fake_entities)
        add_memories_mock = AsyncMock()
        monkeypatch.setattr("services.mem0.ingest.add_memories", add_memories_mock)

        summary = await replay.abstract_schemas_for_user("u1", dry_run=False)

        assert summary["groups_checked"] == 0
        assert summary["schemas_created"] == 0
        add_memories_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_generates_and_inserts_with_provenance(self, monkeypatch):
        rows = [
            {"id": f"f{i}", "memory": f"作品{i}が好き",
             "metadata": {"category": "hobby", "subcategory": "movie"}}
            for i in range(3)
        ]
        monkeypatch.setattr(
            "services.mem0.client.get_all_memories", AsyncMock(return_value=rows),
        )

        def _fake_entities(fid):
            return [_entity(int(fid[1:]), "media")]

        monkeypatch.setattr("services.mem0.graph.get_fact_entities", _fake_entities)

        async def _fake_generate(texts):
            assert len(texts) == 3
            return ["SF作品が好き"]

        monkeypatch.setattr(replay, "_generate_schema_facts", _fake_generate)
        add_memories_mock = AsyncMock(return_value={"kept": 1, "results": []})
        monkeypatch.setattr("services.mem0.ingest.add_memories", add_memories_mock)

        summary = await replay.abstract_schemas_for_user("u1", dry_run=False)

        assert summary["groups_checked"] == 1
        assert summary["schemas_created"] == 1
        add_memories_mock.assert_called_once()
        kwargs = add_memories_mock.call_args.kwargs
        fact = kwargs["pre_extracted_facts"][0]
        assert fact["text"] == "SF作品が好き"
        assert fact["tags"] == ["schema"]
        assert kwargs["metadata"]["source"] == "consolidation_schema"
        assert set(kwargs["metadata"]["derived_from"].split(",")) == {"f0", "f1", "f2"}

    @pytest.mark.asyncio
    async def test_dry_run_never_calls_add_memories(self, monkeypatch):
        rows = [
            {"id": f"f{i}", "memory": f"作品{i}が好き",
             "metadata": {"category": "hobby", "subcategory": "movie"}}
            for i in range(3)
        ]
        monkeypatch.setattr(
            "services.mem0.client.get_all_memories", AsyncMock(return_value=rows),
        )

        def _fake_entities(fid):
            return [_entity(int(fid[1:]), "media")]

        monkeypatch.setattr("services.mem0.graph.get_fact_entities", _fake_entities)

        async def _fake_generate(texts):
            return ["SF作品が好き"]

        monkeypatch.setattr(replay, "_generate_schema_facts", _fake_generate)
        add_memories_mock = AsyncMock()
        monkeypatch.setattr("services.mem0.ingest.add_memories", add_memories_mock)

        summary = await replay.abstract_schemas_for_user("u1", dry_run=True)

        assert summary["schemas_created"] == 1
        add_memories_mock.assert_not_called()

"""TDD Phase 5: semantic search over the raw transcript (history_embed).

Mem0 stores only curated facts and history_index does literal-keyword recall;
neither lets a paraphrased query ("あの水産会社") reach an old raw line. This
module embeds each raw JSONL line into a separate ChromaDB collection so
cosine search can. It is env-gated (default OFF) so per-turn embedding cost is
opt-in. Collection access is mocked here — no real ChromaDB / OpenAI needed.
"""
from unittest.mock import MagicMock, patch

import config
from services.mem0 import history_embed


def _query_result():
    return {
        "documents": [["好きな酒はウイスキー", "猫が好き"]],
        "metadatas": [[
            {"ts": "2026-06-01T10:00", "role": "user", "author": "haroin", "channel_id": "1"},
            {"ts": "2026-06-02T11:00", "role": "channel", "author": "haroin", "channel_id": "1"},
        ]],
        "distances": [[0.12, 0.30]],
    }


class TestIndexLine:
    def test_indexes_when_enabled(self):
        col = MagicMock()
        with patch.object(config, "MEM0_HISTORY_EMBED_ENABLED", True), \
             patch.object(history_embed, "_get_collection", return_value=col):
            history_embed.index_line(1, "2026-06-22T00:00", "user", "haroin",
                                     "好きな酒はウイスキー")
        assert col.upsert.called
        kw = col.upsert.call_args.kwargs
        assert kw["documents"] == ["好きな酒はウイスキー"]
        assert kw["metadatas"][0]["channel_id"] == "1"
        assert kw["metadatas"][0]["author"] == "haroin"

    def test_noop_when_disabled(self):
        col = MagicMock()
        with patch.object(config, "MEM0_HISTORY_EMBED_ENABLED", False), \
             patch.object(history_embed, "_get_collection", return_value=col):
            history_embed.index_line(1, "t", "user", "haroin", "x")
        col.upsert.assert_not_called()

    def test_noop_on_empty_text(self):
        col = MagicMock()
        with patch.object(config, "MEM0_HISTORY_EMBED_ENABLED", True), \
             patch.object(history_embed, "_get_collection", return_value=col):
            history_embed.index_line(1, "t", "user", "haroin", "   ")
        col.upsert.assert_not_called()

    def test_failure_is_swallowed(self):
        col = MagicMock()
        col.upsert.side_effect = RuntimeError("chroma down")
        with patch.object(config, "MEM0_HISTORY_EMBED_ENABLED", True), \
             patch.object(history_embed, "_get_collection", return_value=col):
            # must not raise — indexing is best-effort
            history_embed.index_line(1, "t", "user", "haroin", "ok")

    def test_long_line_truncated_before_embed(self):
        # A line over the embedder's token cap must be truncated, not sent raw
        # (OpenAI 400s on >8192 tokens). Pin the char cap and check the doc.
        col = MagicMock()
        with patch.object(config, "MEM0_HISTORY_EMBED_ENABLED", True), \
             patch.object(config, "MEM0_HISTORY_EMBED_MAX_CHARS", 100), \
             patch.object(history_embed, "_get_collection", return_value=col):
            history_embed.index_line(1, "t", "assistant", "", "あ" * 5000)
        doc = col.upsert.call_args.kwargs["documents"][0]
        assert len(doc) == 100


class TestSearchSemantic:
    def test_returns_shaped_and_channel_scoped(self):
        col = MagicMock()
        col.query.return_value = _query_result()
        with patch.object(config, "MEM0_HISTORY_EMBED_ENABLED", True), \
             patch.object(history_embed, "_get_collection", return_value=col):
            out = history_embed.search_semantic("酒の好み", channel_id=1, limit=2)
        # query is scoped to the channel
        assert col.query.call_args.kwargs["where"] == {"channel_id": "1"}
        assert col.query.call_args.kwargs["n_results"] == 2
        assert len(out) == 2
        assert out[0]["memory"] == "好きな酒はウイスキー"
        assert out[0]["author"] == "haroin"
        assert "score" in out[0]

    def test_noop_when_disabled(self):
        with patch.object(config, "MEM0_HISTORY_EMBED_ENABLED", False):
            assert history_embed.search_semantic("q", channel_id=1) == []

    def test_failure_returns_empty(self):
        col = MagicMock()
        col.query.side_effect = RuntimeError("chroma down")
        with patch.object(config, "MEM0_HISTORY_EMBED_ENABLED", True), \
             patch.object(history_embed, "_get_collection", return_value=col):
            assert history_embed.search_semantic("q", channel_id=1) == []

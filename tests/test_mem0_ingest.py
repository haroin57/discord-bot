"""Unit tests for services/mem0/ingest.py — all external deps are mocked."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

USER_ID = "573489206240870410"


def _make_mem_mock():
    """Return a MagicMock that behaves like a Mem0 Memory instance."""
    m = MagicMock()
    m.add.return_value = {"results": [{"id": "abc"}]}
    return m


def _patch_config(**overrides):
    """Return a dict of config attribute patches (gate on by default)."""
    defaults = {
        "config.MEM0_GATE_ENABLED": True,
        "config.MEM0_GATE_MIN_IMPORTANCE": 3,
        "config.MEM0_CLIENT_DEDUP_ENABLED": False,
        "config.MEM0_CLIENT_DEDUP_THRESHOLD": 0.85,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mem_mock():
    return _make_mem_mock()


@pytest.fixture()
def patch_deps(mem_mock):
    """Patch the three external call points that ingest.py touches."""
    with (
        patch("services.mem0.ingest._get_mem0", return_value=mem_mock),
        patch("services.mem0.ingest._refresh_mem0_client_token"),
        patch("services.mem0.ingest.ensure_valid_token", new_callable=AsyncMock),
        patch("services.mem0.ingest.search_memories", new_callable=AsyncMock) as mock_search,
    ):
        mock_search.return_value = []
        yield {"mem": mem_mock, "search": mock_search}


# ---------------------------------------------------------------------------
# Gate path (MEM0_GATE_ENABLED=True)
# ---------------------------------------------------------------------------

class TestAddMemoriesGatePath:
    @pytest.mark.asyncio
    async def test_normal_add_kept_fact(self, patch_deps):
        """A self-speaker fact above min_importance gets raw-inserted."""
        facts = [{"speaker": "self", "text": "好きな食べ物はラーメン", "importance": 4,
                  "category": "personal", "subcategory": "food", "tags": ["ramen"]}]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="ラーメンが好きです", assistant_text="そうですか", user_id=USER_ID)

        assert result is not None
        assert result["kept"] == 1
        assert result["skipped"]["low_importance"] == 0
        assert result["skipped"]["other_speaker"] == 0
        patch_deps["mem"].add.assert_called_once()

    @pytest.mark.asyncio
    async def test_low_importance_fact_skipped(self, patch_deps):
        """Facts below MEM0_GATE_MIN_IMPORTANCE are dropped."""
        facts = [{"speaker": "self", "text": "今日は天気がいい", "importance": 1,
                  "category": "meta", "subcategory": "chat", "tags": []}]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="今日は天気がいい", user_id=USER_ID)

        assert result["kept"] == 0
        assert result["skipped"]["low_importance"] == 1
        patch_deps["mem"].add.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_speaker_fact_skipped(self, patch_deps):
        """Facts with speaker != 'self' are dropped."""
        facts = [{"speaker": "other", "text": "友達が長野に住んでいる", "importance": 4,
                  "category": "personal", "subcategory": "friend", "tags": []}]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="友達が来た", user_id=USER_ID)

        assert result["kept"] == 0
        assert result["skipped"]["other_speaker"] == 1

    @pytest.mark.asyncio
    async def test_intra_batch_dedup(self, patch_deps):
        """Two facts with identical text in the same batch — only one is kept."""
        facts = [
            {"speaker": "self", "text": "長野市在住", "importance": 4, "category": "personal", "subcategory": "loc", "tags": []},
            {"speaker": "self", "text": "長野市在住", "importance": 4, "category": "personal", "subcategory": "loc", "tags": []},
        ]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="長野市に住んでいる", user_id=USER_ID)

        assert result["kept"] == 1
        assert result["skipped"]["near_dup"] == 1

    @pytest.mark.asyncio
    async def test_empty_facts_returns_zero_kept(self, patch_deps):
        """If extract returns empty list, kept=0 and no insert is called."""
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": [], "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="あ", user_id=USER_ID)

        assert result["kept"] == 0
        patch_deps["mem"].add.assert_not_called()

    @pytest.mark.asyncio
    async def test_metadata_keys_attached(self, patch_deps):
        """Inserted metadata must include category, subcategory, importance, tags."""
        facts = [{"speaker": "self", "text": "Rustを勉強している", "importance": 4,
                  "category": "tech", "subcategory": "rust", "tags": ["rust", "learning"]}]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            await add_memories(user_text="Rustを勉強中", user_id=USER_ID)

        call_kwargs = patch_deps["mem"].add.call_args
        meta = call_kwargs.kwargs["metadata"]
        assert meta["category"] == "tech"
        assert meta["subcategory"] == "rust"
        assert meta["importance"] == 4
        assert "rust" in meta["tags"]
        assert meta["gate_version"] == "v2"
        assert "timestamp" in meta

    @pytest.mark.asyncio
    async def test_gate_v1_label_when_gate_disabled_legacy_path(self, patch_deps):
        """Legacy path sets gate_version=v1 in base_meta."""
        patch_deps["mem"].add.return_value = {"results": [{"id": "x"}]}
        with (
            patch("config.MEM0_GATE_ENABLED", False),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="hello", assistant_text="world", user_id=USER_ID)

        assert result is not None
        call_kwargs = patch_deps["mem"].add.call_args
        meta = call_kwargs.kwargs["metadata"]
        assert meta["gate_version"] == "v1"


# ---------------------------------------------------------------------------
# Dedup via search_memories (MEM0_CLIENT_DEDUP_ENABLED=True)
# ---------------------------------------------------------------------------

class TestClientDedup:
    @pytest.mark.asyncio
    async def test_near_dup_search_match_drops_fact(self, patch_deps):
        """If search returns a result within hard-drop distance, fact is dropped.

        Uses score=0.05 (distance under DEDUP_HARD_DROP_DISTANCE=0.15) so the
        2-stage dedup short-circuits without calling Haiku. mem0's Chroma
        adapter returns *distance* not similarity — small score = duplicate."""
        facts = [{"speaker": "self", "text": "TypeScriptが好き", "importance": 4,
                  "category": "tech", "subcategory": "ts", "tags": []}]
        patch_deps["search"].return_value = [{"score": 0.05}]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", True),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="TSが好き", user_id=USER_ID)

        assert result["kept"] == 0
        assert result["skipped"]["near_dup"] == 1
        patch_deps["mem"].add.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_below_threshold_keeps_fact(self, patch_deps):
        """If search score is below threshold, fact is kept and inserted."""
        facts = [{"speaker": "self", "text": "Pythonが得意", "importance": 4,
                  "category": "tech", "subcategory": "python", "tags": []}]
        patch_deps["search"].return_value = [{"score": 0.50}]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", True),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="Pythonが得意", user_id=USER_ID)

        assert result["kept"] == 1
        patch_deps["mem"].add.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_exception_fail_open(self, patch_deps):
        """If search raises, the fact is fail-open kept (not dropped)."""
        facts = [{"speaker": "self", "text": "GoLangを使う", "importance": 4,
                  "category": "tech", "subcategory": "go", "tags": []}]
        patch_deps["search"].side_effect = RuntimeError("chroma down")
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", True),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="Goを使う", user_id=USER_ID)

        assert result["kept"] == 1


# ---------------------------------------------------------------------------
# Legacy path (MEM0_GATE_ENABLED=False)
# ---------------------------------------------------------------------------

class TestLegacyPath:
    @pytest.mark.asyncio
    async def test_legacy_messages_list(self, patch_deps):
        """Legacy path: messages list is filtered and passed to mem.add."""
        patch_deps["mem"].add.return_value = {"results": [{"id": "1"}, {"id": "2"}]}
        msgs = [
            {"role": "system", "content": "context"},
            {"role": "user", "content": "こんにちは"},
            {"role": "assistant", "content": "はい"},
        ]
        with (
            patch("config.MEM0_GATE_ENABLED", False),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(messages=msgs, user_id=USER_ID)

        assert result is not None
        call_args = patch_deps["mem"].add.call_args
        passed_msgs = call_args.args[0]
        # system role should be filtered out
        roles = [m["role"] for m in passed_msgs]
        assert "system" not in roles
        assert "user" in roles
        assert "assistant" in roles

    @pytest.mark.asyncio
    async def test_legacy_fallback_to_user_assistant_text(self, patch_deps):
        """Legacy path with no messages uses user_text/assistant_text."""
        patch_deps["mem"].add.return_value = {"results": [{"id": "x"}]}
        with (
            patch("config.MEM0_GATE_ENABLED", False),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="テキスト", assistant_text="返答", user_id=USER_ID)

        call_args = patch_deps["mem"].add.call_args
        passed_msgs = call_args.args[0]
        assert any(m["role"] == "user" for m in passed_msgs)
        assert any(m["role"] == "assistant" for m in passed_msgs)


# ---------------------------------------------------------------------------
# Signature normalization (messages → user_text/assistant_text)
# ---------------------------------------------------------------------------

class TestSignatureNormalization:
    @pytest.mark.asyncio
    async def test_messages_converted_to_user_assistant_text(self, patch_deps):
        """messages list is parsed into user_text / assistant_text for gate path."""
        facts_seen = {}

        async def capture_extract(user_text, assistant_text, context_hint="", **kwargs):
            facts_seen["user_text"] = user_text
            facts_seen["assistant_text"] = assistant_text
            return {"facts": [], "relations": []}

        msgs = [
            {"role": "user", "content": "ユーザー発話"},
            {"role": "assistant", "content": "アシスタント応答"},
        ]
        with (
            patch("services.mem0.ingest.extract_full_signals", side_effect=capture_extract),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            await add_memories(messages=msgs, user_id=USER_ID)

        assert facts_seen["user_text"] == "ユーザー発話"
        assert facts_seen["assistant_text"] == "アシスタント応答"

    @pytest.mark.asyncio
    async def test_system_message_sets_context_hint(self, patch_deps):
        """role=system in messages list sets context_hint for gate extract."""
        hints_seen = {}

        async def capture_extract(user_text, assistant_text, context_hint="", **kwargs):
            hints_seen["context_hint"] = context_hint
            return {"facts": [], "relations": []}

        msgs = [
            {"role": "system", "content": "チャンネル履歴"},
            {"role": "user", "content": "hello"},
        ]
        with (
            patch("services.mem0.ingest.extract_full_signals", side_effect=capture_extract),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            await add_memories(messages=msgs, user_id=USER_ID)

        assert hints_seen["context_hint"] == "チャンネル履歴"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_no_mem0_returns_none(self):
        """If _get_mem0() returns None, add_memories returns None immediately."""
        with (
            patch("services.mem0.ingest._get_mem0", return_value=None),
            patch("services.mem0.ingest.ensure_valid_token", new_callable=AsyncMock),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="test", user_id=USER_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_input_gate_path(self, patch_deps):
        """Empty user_text and assistant_text → extract called with empty strings."""
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": [], "relations": []}) as mock_extract,
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_id=USER_ID)

        mock_extract.assert_awaited_once()
        assert result["kept"] == 0

    @pytest.mark.asyncio
    async def test_huge_input_does_not_crash(self, patch_deps):
        """Very large input string is handled without error."""
        big_text = "x" * 100_000
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": [], "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text=big_text, user_id=USER_ID)

        assert result is not None

    @pytest.mark.asyncio
    async def test_ensure_valid_token_exception_returns_none(self, mem_mock):
        """If ensure_valid_token raises, the outer except surfaces the error.

        (2026-07-04: was `return None`, which made every failure invisible
        through the MCP server — now the reason rides in an ``error`` key.)
        """
        with (
            patch("services.mem0.ingest._get_mem0", return_value=mem_mock),
            patch("services.mem0.ingest._refresh_mem0_client_token"),
            patch("services.mem0.ingest.ensure_valid_token", new_callable=AsyncMock, side_effect=RuntimeError("token fail")),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="test", user_id=USER_ID)

        assert result["error"] == "RuntimeError: token fail"
        assert result["kept"] == 0

    @pytest.mark.asyncio
    async def test_caller_metadata_allowed_keys_merged(self, patch_deps):
        """Allowed caller metadata keys (channel_id, source) are passed through."""
        facts = [{"speaker": "self", "text": "テスト", "importance": 4,
                  "category": "meta", "subcategory": "test", "tags": []}]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            await add_memories(
                user_text="テスト",
                user_id=USER_ID,
                metadata={"channel_id": "999", "source": "discord", "evil_key": "injection"},
            )

        meta = patch_deps["mem"].add.call_args.kwargs["metadata"]
        assert meta.get("channel_id") == "999"
        assert meta.get("source") == "discord"
        # evil_key must not be in metadata
        assert "evil_key" not in meta

    @pytest.mark.asyncio
    async def test_insert_failure_does_not_propagate(self, patch_deps):
        """If mem.add raises, _insert_one catches it and returns None (no crash)."""
        patch_deps["mem"].add.side_effect = RuntimeError("chroma write fail")
        facts = [{"speaker": "self", "text": "テスト失敗", "importance": 4,
                  "category": "meta", "subcategory": "test", "tags": []}]
        with (
            patch("services.mem0.ingest.extract_full_signals", new_callable=AsyncMock, return_value={"facts": facts, "relations": []}),
            patch("config.MEM0_GATE_ENABLED", True),
            patch("config.MEM0_GATE_MIN_IMPORTANCE", 3),
            patch("config.MEM0_CLIENT_DEDUP_ENABLED", False),
            patch("config.MEM0_CLIENT_DEDUP_THRESHOLD", 0.85),
        ):
            from services.mem0.ingest import add_memories
            result = await add_memories(user_text="テスト", user_id=USER_ID)

        assert result is not None
        assert result["kept"] == 1
        assert result["results"] == []

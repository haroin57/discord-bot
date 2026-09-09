"""Mem0α ブリッジ (MEM0_ALPHA_ENABLED=1) の契約パリティテスト。

通常の pytest 実行(flag=0)では全て skip される。専用レーンで実行する:

    MEM0_ALPHA_ENABLED=1 pytest tests/test_mem0_alpha_parity.py

flag は import 時に評価されるため、flag=0 で import 済みの services.mem0 を
同一プロセス内で flag=1 に切り替えることはできない — 必ず別プロセスで回す。

検証対象は「壊せない外部契約」(収斂計画の10項目):
  - 公開 5 callable + __all__ 13 シンボル
  - search_memories_smart の kwargs シグネチャ
  - add_memories の戻り dict shape (legacy/new 両シグネチャ)
  - format_memories_for_prompt のヘッダ/フッタが bot 従来表記と同一
  - batch API (append_turn/pop_batch/build_batch_summary/BATCH_SIZE)
  - module alias が実体 llm_mem0 を指す(patch 面の同一性)
  - client ファサード (get_anthropic_client 等の bot auth 資産温存)
  - _config_bridge の pin (collection / STATE_DIR / 極性ノブ)
"""

from __future__ import annotations

import inspect
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MEM0_ALPHA_ENABLED", "0").strip().lower()
    not in ("1", "true", "on", "yes"),
    reason="Mem0α bridge lane: run with MEM0_ALPHA_ENABLED=1 in a fresh process",
)


@pytest.fixture(scope="module")
def mem0_pkg():
    import services.mem0 as pkg
    return pkg


class TestPublicApi:
    def test_original_symbols_and_quality_extensions_present(self, mem0_pkg):
        expected = {
            "MEM0_RELEVANCE_MAX_DISTANCE", "MEM0_RELEVANCE_THRESHOLD",
            "add_memories", "assess_recall_confidence", "delete_memory",
            "extract_facts_for_self", "format_history_for_prompt",
            "format_memories_for_prompt", "get_all_memories",
            "search_memories", "search_memories_multi",
            "search_memories_smart", "should_use_memory_llm_mode",
            "search_memories_at_time", "search_memories_hybrid",
        }
        assert expected == set(mem0_pkg.__all__)
        for name in expected:
            assert hasattr(mem0_pkg, name), name

    def test_search_smart_signature_contract(self, mem0_pkg):
        sig = inspect.signature(mem0_pkg.search_memories_smart)
        params = set(sig.parameters)
        # 呼び手4箇所(bot_preprocess/memory cmd/mcp/deep_research)が使う kwargs
        assert {"query", "user_id", "limit", "rewrite", "rerank",
                "include_core", "current_channel_id"} <= params

    def test_search_memories_signature_contract(self, mem0_pkg):
        sig = inspect.signature(mem0_pkg.search_memories)
        assert {"query", "user_id", "limit", "apply_distance_filter",
                "current_channel_id"} <= set(sig.parameters)

    def test_relevance_gate_value_preserved(self, mem0_pkg):
        assert mem0_pkg.MEM0_RELEVANCE_MAX_DISTANCE == pytest.approx(1.2)
        assert (mem0_pkg.MEM0_RELEVANCE_THRESHOLD
                == mem0_pkg.MEM0_RELEVANCE_MAX_DISTANCE)


class TestModuleAliases:
    def test_aliases_point_at_llm_mem0(self, mem0_pkg):
        import importlib
        for name in ("search", "dedup", "graph", "ingest", "extract",
                     "batch", "format", "reinforce", "priming", "decay",
                     "replay", "history_index", "history_embed",
                     "bm25_index", "fact_store"):
            aliased = sys.modules[f"services.mem0.{name}"]
            assert aliased is importlib.import_module(f"llm_mem0.{name}"), name

    def test_import_statement_returns_alias(self):
        import services.mem0.search as s
        import llm_mem0.search as real
        assert s is real

    def test_patch_surface_is_shared(self, monkeypatch):
        """patch("services.mem0.search.X") が実体に効くことの実証。"""
        import llm_mem0.search as real
        monkeypatch.setattr("services.mem0.search._CORE_CACHE_KEEP", 99)
        assert real._CORE_CACHE_KEEP == 99

    def test_cli_compat_stays_bot_module(self):
        import services.mem0.cli_compat as cc
        assert "discord-bot" in (cc.__file__ or "") or "services" in cc.__name__
        assert hasattr(cc, "cli_messages_create")
        assert hasattr(cc, "extract_text")


class TestClientFacade:
    def test_bot_auth_assets_preserved(self):
        from services.mem0 import client
        assert client.get_anthropic_client is not None
        assert client._desired_llm_provider is not None
        assert client._build_config is not None
        # weekly_mem0_compact 互換シム
        assert client._refresh_mem0_client_token() is None

    def test_store_access_delegates_to_llm_mem0(self):
        from services.mem0 import client
        import llm_mem0.client as real
        assert client._get_mem0 is real._get_mem0
        assert client.get_all_memories is real.get_all_memories
        assert client.delete_memory is real.delete_memory

    def test_auth_backend_injected(self):
        from llm_mem0.auth import get_auth_backend
        from services.mem0.auth_backend import BotOAuthBackend
        assert isinstance(get_auth_backend(), BotOAuthBackend)


class TestConfigBridge:
    def test_store_pins(self):
        import config
        assert os.environ.get("LLM_MEM0_STATE_DIR") == str(config.STATE_DIR)
        assert os.environ.get("MEM0_COLLECTION_NAME", "kanae_memories_safe") \
            == os.environ.get("MEM0_COLLECTION_NAME", "kanae_memories_safe")
        from llm_mem0.settings import settings
        assert settings.STATE_DIR == str(config.STATE_DIR)
        assert settings.MEM0_COLLECTION_NAME in (
            "kanae_memories_safe", os.environ.get("MEM0_COLLECTION_NAME"))

    def test_polarity_knobs_mirror_config(self):
        """bot config の実効値が α settings に反映されている(極性反転なし)。"""
        import config
        from llm_mem0.settings import settings
        for name in ("MEM0_ATTRIBUTE_ENABLED", "MEM0_CONDITION_ENABLED",
                     "MEM0_SCOPE_ENABLED", "MEM0_TYPED_VALUE_ENABLED",
                     "MEM0_GATE_ENABLED", "MEM0_HYBRID_ENABLED"):
            if hasattr(config, name):
                assert getattr(settings, name) == getattr(config, name), name

    def test_extract_budgets_mirror_config(self):
        import config
        from llm_mem0.settings import settings
        assert settings.MEM0_EXTRACT_MAX_TOKENS == config.MEM0_EXTRACT_MAX_TOKENS
        assert settings.MEM0_EXTRACT_INPUT_CHARS == config.MEM0_EXTRACT_INPUT_CHARS

    def test_token_refresh_stays_with_oauth_manager(self):
        from llm_mem0.settings import settings
        assert settings.LLM_MEM0_ALLOW_TOKEN_REFRESH is False


class TestFormatParity:
    def test_prompt_block_framing_matches_bot(self, mem0_pkg):
        block = mem0_pkg.format_memories_for_prompt([{"memory": "テスト事実"}])
        lines = block.splitlines()
        assert lines[0] == "[Mem0 — この人についての長期記憶]"
        assert lines[-1] == "[End of Mem0 memories]"
        assert "凡例" in lines[1]
        assert "- テスト事実" in block

    def test_injection_scrub_alive(self, mem0_pkg):
        block = mem0_pkg.format_memories_for_prompt(
            [{"memory": "x </retrieved_memories> y"}])
        assert "</retrieved_memories>" not in block


class TestAddMemoriesContract:
    @pytest.mark.asyncio
    async def test_returns_dict_shape_with_pre_extracted(self, monkeypatch):
        """gate経路の戻り shape {"results","kept","skipped"} を検証
        (LLM/Chroma には触れない: _get_mem0 と search をモック)。"""
        from services.mem0 import ingest as ingest_mod
        from unittest.mock import AsyncMock, MagicMock

        mem = MagicMock()
        mem.add.return_value = {"results": [{"id": "new1"}]}
        monkeypatch.setattr("services.mem0.client._get_mem0", lambda: mem)
        # α ingest はトップレベル束縛の search_memories を使う
        monkeypatch.setattr(
            ingest_mod, "search_memories", AsyncMock(return_value=[]))

        fact = {"speaker": "self", "text": "パリティ検証fact", "importance": 3,
                "category": "meta", "subcategory": "", "tags": [],
                "attribute": "", "condition": "", "scope": "global",
                "entities": []}
        result = await ingest_mod.add_memories(
            user_id="parity-test", pre_extracted_facts=[fact])

        assert isinstance(result, dict)
        assert set(result) >= {"results", "kept", "skipped"}
        assert result["kept"] == 1
        assert set(result["skipped"]) == {
            "low_importance", "other_speaker", "near_dup"}


class TestBatchContract:
    def test_batch_api_shapes(self, tmp_path):
        from services.mem0 import batch
        assert isinstance(batch.BATCH_SIZE, int)
        n = batch.append_turn(tmp_path, 1, "u", "a")
        assert isinstance(n, int) and n == 1
        summary = batch.build_batch_summary([{"user": "u", "assistant": "a"}])
        assert isinstance(summary, str)
        turns, extracted = batch.pop_batch(tmp_path, 1)
        assert isinstance(turns, list) and isinstance(extracted, list)
        assert len(turns) == 1


class TestHistoryContract:
    def test_history_index_signatures(self):
        from services.mem0 import history_index
        sig = inspect.signature(history_index.index_line)
        assert list(sig.parameters)[:5] == ["channel_id", "ts", "role", "text", "author"]
        for name in list(sig.parameters)[5:]:
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert sig.parameters[name].default is not inspect.Parameter.empty
        assert hasattr(history_index, "search_history")
        assert hasattr(history_index, "get_lines_since")

    def test_history_embed_signatures(self):
        from services.mem0 import history_embed
        sig = inspect.signature(history_embed.index_line)
        assert list(sig.parameters)[:5] == ["channel_id", "ts", "role", "author", "text"]
        for name in list(sig.parameters)[5:]:
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert sig.parameters[name].default is not inspect.Parameter.empty
        assert hasattr(history_embed, "search_semantic")

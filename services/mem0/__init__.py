"""Mem0 memory service package — semantic long-term memory for the Discord bot.

Public API re-exports for `from services.mem0 import ...`.

**MEM0_ALPHA_ENABLED=1** (opt-in, 既定OFF) で実装を Mem0α ライブラリ
(pip: mem0-alpha / import: llm_mem0) に切り替える。切替は import 時に
sys.modules エイリアスで行うため、`services.mem0.search` 等のモジュール
パス・公開 API・テストの patch 面は不変のまま実体だけが llm_mem0 になる。
bot 固有の auth (oauth_manager + cli_compat 課金ルーティング +
DeepSeek フォールバック) は BotOAuthBackend として llm_mem0 に注入し、
設定は _config_bridge が config 実効値を llm_mem0 settings へミラーする。
ストア(Chroma collection / .state sqlite 群)は同一のものを指す。

flag=0 (既定) は従来実装のままで、このファイルの else 分岐以外は一切
実行されない。切り戻しは .env の MEM0_ALPHA_ENABLED=0 + 再起動のみ。

Internal modules:
  client   — singleton + OAuth + config (also low-level get_all/delete)
  ingest   — add_memories (P2 gate + legacy fallback)
  search   — search/search_multi/search_smart + rewrite/rerank
  extract  — extract_facts_for_self + _classify_for_metadata
  dedup    — two-stage dedup (cosine + LLM judgment)
  format   — format_memories_for_prompt
"""

import os


def _alpha_enabled() -> bool:
    return os.environ.get("MEM0_ALPHA_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
    )


if (
    _alpha_enabled()
    and os.environ.get("MEM0_INTEGRITY_MODE", "legacy").lower() != "legacy"
):
    raise RuntimeError(
        "MEM0_ALPHA_ENABLED cannot be combined with shadow/canonical integrity mode",
    )


if _alpha_enabled():
    # ======================================================================
    # Mem0α (llm_mem0) 収斂経路 — 順序が重要:
    # ①設定ブリッジ → ②auth注入 → ③旧client実体ロード+ファサードbind →
    # ④moduleエイリアス → ⑤公開API再エクスポート
    # ======================================================================
    import importlib
    import logging as _logging
    import sys

    from services.mem0 import _config_bridge

    _config_bridge.apply()

    from llm_mem0.auth import set_auth_backend
    from services.mem0.auth_backend import BotOAuthBackend

    set_auth_backend(BotOAuthBackend())

    # 旧 client 実体(services/mem0/client.py)を本来の名前で先にロードして
    # bot 固有 auth 資産を確保し、client ファサードに注入してから
    # sys.modules を差し替える。
    from services.mem0 import client as _client_orig
    from services.mem0 import client_alpha as _client_facade

    _client_facade.bind(_client_orig)

    _this = sys.modules[__name__]

    # llm_mem0 のモジュールをそのまま services.mem0.* として見せる。
    # `import services.mem0.search` も `patch("services.mem0.search.X")` も
    # 実体 llm_mem0.search の名前空間に届く。cli_compat は bot 実体のまま
    # (課金ルーティング — mem0 外の7ファイルも利用)、client はファサード。
    _ALPHA_ALIASES = (
        "search",
        "dedup",
        "conflict",
        "morpho",
        "bm25_index",
        "attribute_registry",
        "fact_store",
        "graph",
        "history_embed",
        "history_index",
        "reinforce",
        "priming",
        "decay",
        "replay",
        "format",
        "ingest",
        "extract",
        "batch",
        "llm",
        "settings",
        "sqlite_store",
        "atomic_write",
        "sanitize",
    )
    for _name in _ALPHA_ALIASES:
        _mod = importlib.import_module(f"llm_mem0.{_name}")
        sys.modules[f"services.mem0.{_name}"] = _mod
        setattr(_this, _name, _mod)

    sys.modules["services.mem0.client"] = _client_facade
    _this.client = _client_facade

    # Modules in llm_mem0 bind ``_get_mem0`` at import time. Route those
    # bindings through the facade dynamically so the documented
    # ``services.mem0.client._get_mem0`` monkeypatch surface remains effective.
    def _get_mem0_via_facade():
        return _client_facade._get_mem0()

    for _name in ("ingest", "search", "decay", "replay"):
        _module = getattr(_this, _name, None)
        if _module is not None and hasattr(_module, "_get_mem0"):
            _module._get_mem0 = _get_mem0_via_facade

    from llm_mem0 import (
        add_memories,
        assess_recall_confidence,
        extract_facts_for_self,
        format_history_for_prompt,
        format_memories_for_prompt,
        search_memories,
        search_memories_at_time,
        search_memories_hybrid,
        search_memories_multi,
        search_memories_smart,
        should_use_memory_llm_mode,
    )
    from services.mem0.client_alpha import (
        MEM0_RELEVANCE_MAX_DISTANCE,
        MEM0_RELEVANCE_THRESHOLD,
        delete_memory,
        get_all_memories,
    )

    _logging.getLogger(__name__).info(
        "services.mem0: Mem0α bridge ACTIVE "
        "(llm_mem0 aliased, BotOAuthBackend injected)"
    )
else:
    from services.mem0.client import (
        MEM0_RELEVANCE_MAX_DISTANCE,
        MEM0_RELEVANCE_THRESHOLD,  # back-compat alias; see client.py
        delete_memory,
        get_all_memories,
    )
    from services.mem0.extract import extract_facts_for_self
    from services.mem0.format import (
        format_history_for_prompt,
        format_memories_for_prompt,
    )
    from services.mem0.ingest import add_memories
    from services.mem0.search import (
        assess_recall_confidence,
        search_memories,
        search_memories_multi,
        search_memories_smart,
        should_use_memory_llm_mode,
    )

from services.mem0.integrity import (
    CanonicalRepository,
    MemoryApplicationService,
    ensure_projection_consumer,
    get_application_service,
    get_repository,
    shutdown_projection_consumers,
)

__all__ = [
    "MEM0_RELEVANCE_MAX_DISTANCE",
    "MEM0_RELEVANCE_THRESHOLD",
    "add_memories",
    "assess_recall_confidence",
    "delete_memory",
    "extract_facts_for_self",
    "format_history_for_prompt",
    "format_memories_for_prompt",
    "get_all_memories",
    "search_memories",
    "search_memories_multi",
    "search_memories_smart",
    "should_use_memory_llm_mode",
]

if _alpha_enabled():
    __all__ += ["search_memories_at_time", "search_memories_hybrid"]

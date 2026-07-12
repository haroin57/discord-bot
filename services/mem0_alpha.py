"""Mem0α (pip: mem0-alpha, import: llm_mem0) の opt-in アダプタ。

既存の記憶システム (services/mem0/*) とは**完全に独立**で、既定では無効。
このモジュールの追加以外に既存コードへの変更は一切ない。

非干渉の設計:
- 既定で無効。.env に ``MEM0_ALPHA_ENABLED=1`` を設定した時だけ属性解決が通る
  （無効のまま触ると RuntimeError で案内を出す。llm_mem0 の import も起きない）
- llm_mem0 の既定ストアは collection="memories" / state=~/.llm_mem0/state。
  既存システム (collection=kanae_memories_safe, STATE_DIR=.state) とは
  ChromaDB コレクションも SQLite ファイルも重ならない
- .env の MEM0_*_MODEL 等は llm_mem0 も同名で読むため、モデル設定は
  既存システムと自然に揃う（settings は動的読みなので再起動不要）

パッケージ実体: /home/ubuntu/mem0-work/Mem0-alpha に editable 導入
(``pip install --no-deps -e``、依存バージョンは venv 既存のものを一切変更しない)。

使い方::

    from services import mem0_alpha
    if mem0_alpha.enabled():
        hits = await mem0_alpha.search_memories_smart(query, user_id)
        block = mem0_alpha.format_memories_for_prompt(hits)
"""

from __future__ import annotations

import os

_PUBLIC = (
    "add_memories",
    "search_memories",
    "search_memories_multi",
    "search_memories_smart",
    "should_use_memory_llm_mode",
    "format_memories_for_prompt",
    "format_history_for_prompt",
    "extract_facts_for_self",
    "get_all_memories",
    "delete_memory",
)

__all__ = ["enabled", *_PUBLIC]


def enabled() -> bool:
    """True iff Mem0α is opted in via ``MEM0_ALPHA_ENABLED=1`` (default off)."""
    raw = os.environ.get("MEM0_ALPHA_ENABLED", "0")
    return raw.strip().lower() in ("1", "true", "on", "yes")


def __getattr__(name: str):
    if name in _PUBLIC:
        if not enabled():
            raise RuntimeError(
                "Mem0α adapter is disabled — set MEM0_ALPHA_ENABLED=1 in .env "
                "to opt in. The existing services.mem0 system stays the active "
                "memory backend either way."
            )
        import llm_mem0
        return getattr(llm_mem0, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

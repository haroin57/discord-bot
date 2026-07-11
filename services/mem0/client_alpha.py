"""flag=1 (MEM0_ALPHA_ENABLED) 用の client ファサード。

`services.mem0.__init__` が sys.modules["services.mem0.client"] をこの
モジュールに差し替える。役割は2つ:

1. **ストアアクセスの委譲**: _get_mem0 / get_all_memories / delete_memory /
   関連定数は llm_mem0.client の実体を再エクスポート。
2. **bot 固有 auth 資産の温存**: get_anthropic_client /
   _desired_llm_provider / _build_config / _DEEPSEEK_V4_FLASH は旧
   services/mem0/client.py 実体(bind() で注入)をそのまま公開する。mem0 と
   無関係の7ファイル(services/claude_api/*, invest_kb*)と77箇所のテスト
   patch がこの名前に依存しているため、名前とシグネチャは不変。

_refresh_mem0_client_token は互換シム: α では AuthBackend.refresh_memory_llm
フックがインスタンス払い出しごとに走るため、明示リフレッシュは不要になった
(weekly_mem0_compact.py 等の呼び出し互換のため no-op で残す)。
"""

from __future__ import annotations

import logging

# ストア系は llm_mem0 実体を再エクスポート
from llm_mem0.client import (  # noqa: F401
    MEM0_FINAL_RELEVANCE_MAX_DISTANCE,
    MEM0_RELEVANCE_MAX_DISTANCE,
    MEM0_RELEVANCE_THRESHOLD,
    _get_mem0,
    delete_memory,
    get_all_memories,
    mem_get_all,
    mem_search,
)

log = logging.getLogger(__name__)

# bind() で旧 client 実体から注入される bot 固有 auth 資産。
get_anthropic_client = None
_desired_llm_provider = None
_build_config = None
_DEEPSEEK_V4_FLASH = "deepseek/deepseek-chat-v4-flash"


def bind(orig_client_module) -> None:
    """__init__ のファサード設置時に旧 client 実体の関数を注入する。

    ファサード設置後に `from services.mem0.client import get_anthropic_client`
    すると本モジュール(=sys.modules 上の services.mem0.client)から解決される
    ため、旧実体を直接参照で保持しておく(自己再帰 import を避ける)。
    """
    global get_anthropic_client, _desired_llm_provider, _build_config
    global _DEEPSEEK_V4_FLASH
    get_anthropic_client = orig_client_module.get_anthropic_client
    _desired_llm_provider = orig_client_module._desired_llm_provider
    _build_config = orig_client_module._build_config
    _DEEPSEEK_V4_FLASH = orig_client_module._DEEPSEEK_V4_FLASH


def _refresh_mem0_client_token() -> None:
    """互換シム(no-op)。α では BotOAuthBackend.refresh_memory_llm が
    _get_mem0 の払い出しごとに走るため、明示リフレッシュは不要。"""
    return None

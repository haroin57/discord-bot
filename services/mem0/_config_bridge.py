"""設定ブリッジ — bot の config 実効値を llm_mem0 settings に固定する。

llm_mem0 の settings は「env 直読み・動的」。bot は同名の MEM0_* ノブを
config/__init__.py で定義しており、.env に書かれていないノブは config の
デフォルトが実効値になる。両者のデフォルトには極性違い(例:
MEM0_ATTRIBUTE_ENABLED bot=False / α=True) や値違い(MEM0_EXTRACT_MAX_TOKENS
bot=12000 / α=2000) があるため、無対策で α に切り替えると挙動が反転する。

apply() は llm_mem0 の全ノブ(settings._SPECS)について、env 未設定かつ
config に同名定義があるものを「config の実効値」で os.environ に書き込む
(setdefault 相当 — .env の明示設定は常に勝つ)。これで α は bot と同じ実効値
で動き、ノブの対応表を手で保守する必要がない。

加えて α に対応物のない bot 固有の運用要件を pin する:
- LLM_MEM0_STATE_DIR: bot の .state (sqlite 索引群をそのまま共有)
- MEM0_COLLECTION_NAME: kanae_memories_safe (既存 Chroma collection 継続)
- LLM_MEM0_ALLOW_TOKEN_REFRESH=0: トークン更新は oauth_manager 専権
- MEM0_FORMAT_{HEADER,LEGEND,FOOTER}: プロンプトブロックのマーカーを bot の
  日本語表記に固定(prompt scaffold / sentinel 防御とバイト同一を維持)
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_applied = False


def apply() -> None:
    """冪等。services.mem0 の import 時(MEM0_ALPHA_ENABLED=1)に1回走る。"""
    global _applied
    if _applied:
        return

    import config
    from llm_mem0.settings import _SPECS

    mirrored = 0
    for name in _SPECS:
        if os.environ.get(name) is not None:
            continue  # .env の明示設定が最優先
        val = getattr(config, name, None)
        if val is None:
            continue
        if isinstance(val, bool):
            os.environ[name] = "true" if val else "false"
        else:
            os.environ[name] = str(val)
        mirrored += 1

    # bot 固有 pin (config に同名定義がないもの)
    from config import STATE_DIR
    os.environ.setdefault("LLM_MEM0_STATE_DIR", str(STATE_DIR))
    os.environ.setdefault("MEM0_COLLECTION_NAME", "kanae_memories_safe")
    os.environ.setdefault("LLM_MEM0_ALLOW_TOKEN_REFRESH", "0")
    os.environ.setdefault("MEM0_FORMAT_HEADER", "[Mem0 — この人についての長期記憶]")
    os.environ.setdefault(
        "MEM0_FORMAT_LEGEND",
        "(凡例: imp=重要度1-5 / Live>Hot>Warm>Cold=鮮度tier / "
        "[日付]=最終確認日 / [条件: ...]=その条件下でのみ成り立つ事実)",
    )
    os.environ.setdefault("MEM0_FORMAT_FOOTER", "[End of Mem0 memories]")

    _applied = True
    log.info(
        "mem0-alpha config bridge applied: %d knobs mirrored from config, "
        "collection=%s state_dir=%s",
        mirrored,
        os.environ.get("MEM0_COLLECTION_NAME"),
        os.environ.get("LLM_MEM0_STATE_DIR"),
    )

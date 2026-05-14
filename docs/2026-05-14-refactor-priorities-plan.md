# Discord Bot リファクタ優先順位プラン

- **作成日**: 2026-05-14
- **対象**: `/home/ubuntu/discord-bot` 全体アーキテクチャ
- **目的**: 累積した複雑性を「安全度」で優先順位付けし、段階的に解消する
- **位置付け**: 計画書のみ。実装は別タスクで判断
- **調査手法**: サブエージェント `Explore` 3 体並列走査 + 実コード grep 検証

---

## 0. 全体方針

数か月の累積開発で god class 化と新旧実装の並走が起きている:

- `bot.py` 1255 行、`services/claude_sdk.py` 851 行、`utils/discord_helpers.py` 641 行、`web_api.py` 635 行
- `services/claude_sdk.py` (legacy) ↔ `services/claude_api/` (新) の責務重複
- emotion 系で `transition.py` (algebraic) ↔ `llm_transition.py` (LLM-based) が並走
- atomic write、lock、subprocess 起動など共通パターンが各モジュールで独自実装

リファクタは「動いているものを壊さないこと」が最優先。**Tier S → M → L の順で進める**。
今すぐ動作不能になる問題はゼロ。

---

## 1. Tier 分類と全体像

| Tier | 性質 | 含む項目数 | 工数目安 |
|------|------|----------|---------|
| **S** | 純粋関数抽出、機械的共通化 | 4 | 3-5 時間 |
| **M** | API 変更を伴うが箇所局所 | 3 | 7-9 時間 |
| **L** | アーキテクチャ全体に影響、移行戦略要 | 3 | 数日〜 |

---

## 2. Tier S: 即実行可能 (リスク極小)

### S1. atomic write パターン共通化

**現状**: `uuid.uuid4() + os.replace` の atomic JSON 書き込みが 3 箇所で重複

- [services/session.py:119-134](../services/session.py#L119-L134)
- [services/emotion/state.py:174-181](../services/emotion/state.py#L174-L181)
- [services/emotion/daily.py:78](../services/emotion/daily.py#L78)

**改善案**: `utils/atomic_write.py` 新規

```python
# utils/atomic_write.py
import json, os, uuid
from pathlib import Path
from typing import Any

def atomic_write_json(
    path: Path, data: Any, *, mode: int = 0o600,
    encoding: str = "utf-8", indent: int | None = 2,
) -> None:
    """Atomically write JSON via uuid tmp + os.replace.

    Guarantees the target file is either the old contents or the new
    contents — never partial. Safe under concurrent readers.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding=encoding)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
```

**移行手順**:
1. `utils/atomic_write.py` 新規作成 + `tests/test_atomic_write.py` 単体テスト
2. session.py / emotion/state.py / emotion/daily.py で `from utils.atomic_write import atomic_write_json` し、既存ブロックを置換
3. `pytest tests/test_session.py tests/test_emotion_state.py` で挙動同値性確認

**Safety**: HIGH — 純粋関数、I/O のみ、挙動変更なし

**削減**: 各箇所 ~10 行 × 3 = 30 行 + 共通テスト集約

---

### S2. config.py から MCP selector 分離

**現状**: [config.py:381-450](../config.py#L381) に MCP server 選択ロジックが混在 (~100 行)

- `pick_mcp_servers(raw_user_text)` — keyword scan による MCP server 自動選択
- `get_mcp_servers_by_keys(keys)` — key → config dict 変換
- `_KEYWORDS_*` 各種 keyword set
- `_DOCS_TRIGGERS` documentation 検索トリガー

**呼び出し元** (2 箇所のみ):
- [services/claude_api/entry.py:41-42](../services/claude_api/entry.py#L41-L42)
- [services/claude_sdk.py:51-52](../services/claude_sdk.py#L51-L52)

**改善案**: `services/mcp_selector.py` 新規

```python
# services/mcp_selector.py
from config import MCP_SERVERS  # config は環境設定のみに専念

_KEYWORDS_TWEET = (...)
_KEYWORDS_DOCS = (...)
# ...

def pick_mcp_servers(raw_user_text: str) -> dict: ...
def get_mcp_servers_by_keys(keys: list[str]) -> dict: ...
```

**移行手順**:
1. `services/mcp_selector.py` 新規作成、ロジックをまるごと移動
2. config.py から該当ブロック削除
3. entry.py / claude_sdk.py の import path 変更
4. `pytest tests/test_claude_api_entry.py` で既存テストが通ることを確認

**Safety**: HIGH — 純粋関数、テスト被覆あり

**削減**: config.py から ~100 行除去、関心の分離

---

### S3. utils/discord_helpers.py から TTL+LRU cache 切り出し

**現状**: [utils/discord_helpers.py](../utils/discord_helpers.py) に `_thread_context_cache` (TTL + LRU 実装) がインライン

**改善案**: `utils/cache.py` 新規

```python
# utils/cache.py
from collections import OrderedDict
import time
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

class TTLLRUCache(Generic[K, V]):
    def __init__(self, *, maxsize: int = 128, ttl_s: float = 300.0):
        self._store: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl_s = ttl_s

    def get(self, key: K) -> V | None:
        now = time.monotonic()
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, val = entry
        if now - ts > self._ttl_s:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return val

    def set(self, key: K, val: V) -> None:
        self._store[key] = (time.monotonic(), val)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)
```

**移行手順**:
1. `utils/cache.py` 新規作成 + 単体テスト
2. discord_helpers.py で `_thread_context_cache` を `TTLLRUCache` インスタンスに置換
3. 将来 `_mention_re_cache`, `_keyword_re_cache` も同じパターンに統一可能

**Safety**: HIGH — 内部 helper、外部 API 変更なし

**削減**: discord_helpers.py から ~40 行 + 将来の cache 実装を統一

---

### S4. utils/attachments.py から image validator 切り出し

**現状**: [utils/attachments.py:130-200](../utils/attachments.py#L130) で magic byte check、MIME 検出、size 制限がインライン

**改善案**: `utils/image_validator.py` 新規

```python
# utils/image_validator.py
_IMAGE_MAGIC_PREFIXES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",  # 8-12 byte で再判定
}

_MAX_IMAGE_BYTES = 10 * 1024 * 1024

def identify_image_mime(header_bytes: bytes) -> str | None: ...
def validate_image_bytes(data: bytes) -> tuple[str | None, str]:
    """Returns (mime, error_msg). mime is None when invalid."""
```

**移行手順**:
1. `utils/image_validator.py` 新規作成 + 単体テスト (各 magic byte パターン)
2. attachments.py / claude_api/entry.py の画像処理箇所で import 利用
3. メモリ改善プランの P0-1 (画像サイズ縮小) もここで反映可能

**Safety**: HIGH — 完全に独立した検証ロジック、純粋関数

**削減**: utils/attachments.py から ~100 行除去、claude_api/entry.py の画像処理重複も解消

---

## 3. Tier M: 中安全 (API 変更あり、箇所局所)

### M1. emotion/runtime.py の singleton を context オブジェクト化

**現状**: [services/emotion/runtime.py](../services/emotion/runtime.py) で global `_daemon` 変数

```python
# 現状
_daemon = None

def set_emotion_daemon(daemon): global _daemon; _daemon = daemon
def get_emotion_daemon(): return _daemon
```

テストで mock しにくい、null check が呼び出し元に散在。

**改善案**: `EmotionDaemonContext` クラス化

```python
# services/emotion/runtime.py
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from services.emotion.persistent_session import EmotionSessionDaemon

class EmotionDaemonContext:
    def __init__(self) -> None:
        self._daemon: "EmotionSessionDaemon | None" = None

    def set(self, daemon: "EmotionSessionDaemon | None") -> None:
        self._daemon = daemon

    def get(self) -> "EmotionSessionDaemon | None":
        return self._daemon

    def is_available(self) -> bool:
        return self._daemon is not None and self._daemon.is_alive

    def __enter__(self): return self
    def __exit__(self, *a): self._daemon = None

EMOTION_CTX = EmotionDaemonContext()
```

**変更箇所**:
- bot.py の `set_emotion_daemon()` 呼び出し 2 箇所 (on_ready, _patched_close)
- services/emotion/feedback.py の `get_emotion_daemon()` 呼び出し
- bot.py:309-311 の `_emotion_daemon.is_alive`, `_emotion_daemon.is_conversation_active` チェック

**Safety**: MEDIUM — API 変更あるが箇所局所、後方互換 wrapper 残せば transition 容易

**効果**: テスト容易化 (mock 注入が 1 行)、null check 集約

---

### M2. mem0_service.py レガシー shim 撤去

**現状**: [services/mem0_service.py](../services/mem0_service.py) は 12 行の "LEGACY re-export module"

```python
# 現状
"""LEGACY re-export module. New code should `from services.mem0 import ...` directly."""
from services.mem0 import *
```

利用箇所 (7 ファイル、production):
- [bot.py:42](../bot.py#L42)
- [web_api.py:513,597](../web_api.py#L513)
- [mcp_mem0_server.py:55](../mcp_mem0_server.py#L55)
- [scripts/weekly_mem0_compact.py:39](../scripts/weekly_mem0_compact.py#L39)
- [services/prompt_builder.py:194](../services/prompt_builder.py#L194)
- [services/health.py:45](../services/health.py#L45)

テストでも `patch.dict("sys.modules", {"services.mem0_service": ...})` 形式で多数 patch:
- tests/test_e2e_security.py, test_health.py, test_mcp_mem0_server.py, test_web_api.py, test_prompt_builder.py

**改善案**: 各 import を `from services.mem0 import ...` に書き換え → shim 削除

**移行手順**:
1. production 7 ファイルの import 書き換え
2. テスト 5 ファイルの `patch.dict` 内 module path 更新
3. `services/mem0_service.py` 削除
4. `pytest tests/` 全件 pass 確認

**Safety**: MEDIUM — mechanical だが箇所多い、test patch path も更新

**削減**: shim 12 行 + import path 明確化

---

### M3. services/claude_api/entry.py のサブモジュール抽出

**現状**: [entry.py 488 行](../services/claude_api/entry.py) で `call_with_tools` が以下を全部担当:

- 画像 base64 化 (`_extract_attached_image_blocks`, `_safe_image_path`, line 109-204)
- API client 生成 (`_make_api_client`)
- セッション resume (`_resolve_session`)
- agent loop 実行・retry

**改善案**:

- `services/claude_api/image_processor.py` 新規 — 画像処理を移動 (S4 の `image_validator.py` を利用)
- `services/claude_api/client_factory.py` 新規 — API client 生成ロジックを移動

**移行手順**:
1. image_processor.py 新規 + 単体テスト
2. entry.py から画像処理ブロック削除、import で利用
3. client_factory.py 新規 + 単体テスト
4. entry.py から client 生成削除、import で利用
5. `pytest tests/test_claude_api_entry.py` で既存挙動確認

**Safety**: MEDIUM — 実装複雑、既存 entry.py のテストが薄いので慎重に

**効果**: entry.py が 488 → ~300 行、testability 大幅向上

---

## 4. Tier L: 大規模、将来 (移行戦略要)

### L1. claude_sdk.py 撤去 (legacy SDK backend)

**現状**: `CLAUDE_BACKEND=api` がデフォルト。SDK path は backward compat 用 dispatcher
- 851 行のうち ~500 行が legacy 実装
- 環境変数 `CLAUDE_BACKEND=sdk` を使う運用がなければ撤去可能

**前提条件**:
1. SDK backend 利用者ゼロの確認 (production / dev 環境ともに)
2. dispatcher 経由ではなく直接 `services.claude_api` を呼ぶよう全 caller 移行
3. tests/test_claude_sdk.py の縮減 or 削除

**Safety**: LOW — 運用影響大、段階的移行が必要

**効果**: 851 行削除、新旧並走の混乱解消

---

### L2. bot.py の責務分解

**現状**: 1255 行に以下が混在:
- Discord event handler (on_ready, on_message)
- Prompt 構築 (mention parse, keyword extract, sanitize)
- Rate limit (cooldown, daily quota)
- MCP 統合
- セッション管理
- emotion daemon lifecycle
- vscode_sync 統合

**改善案**:
- `services/message_preprocessor.py` 新規 — keyword/mention parse, sanitize, wrap_untrusted
- rate limit を `services/rate_limit.py` に統合 (既存 module あり、bot.py に重複した logic を集約)
- emotion daemon lifecycle を `services/emotion/runtime.py` の `EmotionDaemonContext` (M1) に完全移譲

**前提条件**: M1 (emotion runtime) 完了

**Safety**: LOW — テスト書き直し大規模、bot.py のグローバル state も整理必要

**効果**: bot.py が 1255 → ~700 行、event handler に集中

**注意**: Phase 5 に進む際は別途 Plan 再策定を推奨

---

### L3. subprocess 起動パターンの共通基盤化

**現状**: subprocess 起動が 5 箇所で独自実装

- [services/emotion/cli.py](../services/emotion/cli.py) — CLI 呼び出し (timeout, JSON parse, retry)
- [services/claude_sdk.py](../services/claude_sdk.py) — asyncio subprocess + retry + orphan cleanup
- [services/claude_api/mcp_client.py](../services/claude_api/mcp_client.py) — AsyncExitStack + stdio_client
- [services/emotion/persistent_session.py](../services/emotion/persistent_session.py) — daemon 起動 + stream-json
- [utils/browser_pool.py](../utils/browser_pool.py) — Puppeteer daemon

**改善案**: `services/subprocess_pool.py` 新規

```python
class ProcessPool:  # abstract
    timeout_s: float
    retry_policy: RetryPolicy
    cleanup_patterns: list[str]  # for orphan cleanup
    async def spawn(self, cmd: list[str]) -> Process: ...
    async def terminate_orphans(self) -> int: ...

class DialogueProcess(ProcessPool):  # short-lived, request-response
    ...

class DaemonProcess(ProcessPool):  # long-lived, health-check
    ...
```

**Safety**: LOW — 5 モジュールに影響、段階的移行が現実的

**効果**: orphan cleanup 重複削除、timeout 一貫性、テスト時間短縮

---

## 5. 訂正セクション (誤検知)

サブエージェント調査で「削除可能」と報告されたが、実コード grep で **安全ではない** と判明した項目:

### 訂正 1: services/emotion/transition.py は削除不可

**Agent 2 の当初主張**: "完全デッド、llm_transition.py が置換済み"

**実態**:
- [scripts/emotion_feedback_cli.py:33,48,88](../scripts/emotion_feedback_cli.py#L33) で `apply_event` を現役利用
- `~/.claude/hooks/emotion-inject.py` が hook 経由で本 CLI を呼ぶ可能性
- CLAUDE.md (project) で CLI 利用方法が明記
- tests/test_emotion_transition.py で挙動テスト維持

**結論**: 削除には emotion_feedback_cli.py 自体の LLM transition 移行が前提。**Tier L 候補に格下げ**

教訓: re-export 経路や CLI script からの import は深い grep が必要。`__init__.py` の `__all__` 経由でしか参照されていない関数も「現役」の場合がある。

---

## 6. 実装ロードマップ

| Phase | 含む項目 | 工数目安 | 順序根拠 |
|-------|---------|---------|---------|
| **Phase 1** | S1 + S3 + S4 | 2-3 h | 純粋関数抽出、テスト書きやすい |
| **Phase 2** | S2 | 1-2 h | テスト被覆あり、import 変更 2 箇所 |
| **Phase 3** | M1 + M2 | 3-4 h | API 変更あり、テスト patch path 更新必要 |
| **Phase 4** | M3 (entry.py 分解) | 4-5 h | S4 (image_validator) 完了後 |
| **Phase 5** | L2 (bot.py 分解) | 1 日+ | M1 完了後、要 Plan 再策定 |
| **Phase 6** | L1 + L3 | 1-2 日 | 運用影響確認後 |

各 Phase 完了後の検証:
1. `pytest tests/` 全件 pass
2. 抽出関数の単体テスト追加
3. `python3 -c "import bot; import web_api"` で circular import / missing symbol 検出
4. bot.py 起動 → on_ready ログ確認 → 1 メッセージ送信して応答確認
5. git commit (各 Phase 1 コミットで切り戻しを容易に)

---

## 7. 新規作成予定ファイル一覧

| Phase | ファイル | 役割 |
|-------|---------|------|
| 1 | `utils/atomic_write.py` | JSON atomic write 共通化 |
| 1 | `utils/cache.py` | TTL+LRU cache クラス |
| 1 | `utils/image_validator.py` | 画像 magic byte / MIME 検証 |
| 2 | `services/mcp_selector.py` | MCP server 自動選択 |
| 3 | (M1) emotion/runtime.py を class 化 | (新規ではなく書き換え) |
| 4 | `services/claude_api/image_processor.py` | 画像 base64 化 + S4 利用 |
| 4 | `services/claude_api/client_factory.py` | API client 生成 |
| 5 | `services/message_preprocessor.py` | bot.py の prompt 構築切り出し |
| 6 | `services/subprocess_pool.py` | subprocess 起動共通基盤 |

---

## 8. 書き換え対象ファイル一覧 (Phase 1-3)

- **Phase 1**:
  - `services/session.py`, `services/emotion/state.py`, `services/emotion/daily.py` — S1 import
  - `utils/discord_helpers.py` — S3 抽出
  - `utils/attachments.py`, `services/claude_api/entry.py` — S4 抽出後の import
- **Phase 2**:
  - `config.py` — MCP selector 関連削除
  - `services/claude_api/entry.py`, `services/claude_sdk.py` — import path 書き換え
- **Phase 3**:
  - `services/emotion/runtime.py`, `bot.py`, `services/emotion/feedback.py` — M1 (context オブジェクト化)
  - bot.py, web_api.py, mcp_mem0_server.py, scripts/weekly_mem0_compact.py, services/prompt_builder.py, services/health.py — M2 (`services.mem0` 直 import)
  - tests/test_e2e_security.py, test_health.py, test_mcp_mem0_server.py, test_web_api.py, test_prompt_builder.py — M2 patch path 更新

---

## 9. 補足: 検査済で問題なしと判定した項目

サブエージェント調査で issue 候補に挙がったが、実コード確認で現状 OK と判定:

- `services/session.py:224-284` `_tail_jsonl` — 既に末尾チャンク読みで最適化済
- discord.py `max_messages=200` — 既に default 1000 から削減済
- MCP subprocess の `AsyncExitStack` 管理 — 適切
- `utils/attachments.cleanup_stale_attachments()` 1h cleanup — 動作 OK
- `services/emotion/persistent_session.py` の単一プロセス常駐 + file source-of-truth — 2GB 環境に最適化済

---

## 10. 関連ドキュメント

- [docs/2026-05-14-memory-safety-improvement-plan.md](2026-05-14-memory-safety-improvement-plan.md) — 同時進行のメモリ改善プラン
- [docs/2026-05-14-agent-sdk-migration-plan.md](2026-05-14-agent-sdk-migration-plan.md) — Agent SDK 移行 (claude_sdk → claude_api の文脈)
- [docs/2026-06-14-emotion-llm-transition-plan.md](2026-06-14-emotion-llm-transition-plan.md) — emotion/transition.py → llm_transition.py 移行
- [docs/2026-05-13-perf-overhaul-and-handover.md](2026-05-13-perf-overhaul-and-handover.md) — 過去のパフォーマンス改善履歴
- [CLAUDE.md](../CLAUDE.md) (project) — Discord bot のキャラクター・出力ルール

---

## 11. 次のステップ

このプランは **計画書のみ** の位置付け。次にどう動くかの選択肢:

1. **Phase 1 から実装着手** — atomic_write / cache / image_validator の 3 つを新規作成、既存箇所を順次置換
2. **Phase ごとに別途 Plan 再策定** — 特に Phase 4 以降 (M3, L2) はテスト戦略含めて再設計が必要
3. **メモリ改善プランと並行進行** — Phase 1 のリファクタと P0-1 (画像サイズ縮小) は同時実施可能 (どちらも `image_validator.py` を触る)

実装着手時は ADR (`docs/adr/`) に判断記録を残すこと。

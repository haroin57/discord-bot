# Discord Bot メモリ改善 + リファクタ 実行プラン

- **作成日**: 2026-05-14
- **位置付け**: 設計ドキュメント (memory-safety / refactor-priorities) を踏まえた**執行計画**
- **対象**: `/home/ubuntu/discord-bot`
- **ブランチ戦略**: feature ブランチ × Phase ごと、PR マージで master 統合

## 関連ドキュメント

- [2026-05-14-memory-safety-improvement-plan.md](2026-05-14-memory-safety-improvement-plan.md) — メモリ改善 設計
- [2026-05-14-refactor-priorities-plan.md](2026-05-14-refactor-priorities-plan.md) — リファクタ優先順位 設計

---

## 確定方針 (ユーザー回答)

| 項目 | 決定 |
|------|------|
| 進行順序 | メモリプラン完了 → リファクタプラン |
| コミット粒度 | Phase 単位 (1 Phase = 1 commit) |
| ブランチ戦略 | feature ブランチを切り PR マージ |
| テスト実行 | Phase 中は関連テストのみ、最後に全件 pytest |
| bot 再起動 | 全 PR マージ後 1 回 |

### 採用項目
**メモリ**: P0-1 (画像 8MB/8枚), P0-2 (sema evict 修正), P1-3 (deque 化), P1-5 (lock 調査), P2-6 (vscode_sync LRU), P2-7 (ingest stream)
**リファクタ**: S1, S2, S3, S4, M1, M3

### スキップ項目
**メモリ**: P1-4 (weekly_mem0_compact チャンク化), P2-8 (smart search siblings)
**リファクタ**: M2 (mem0_service shim 撤去), Tier L 全般

---

## Phase 構成

| Phase | ブランチ名 | 内容 | 工数目安 |
|-------|----------|------|---------|
| 1 | `refactor/memory-p0` | P0-1 + P0-2 | 1-2 h |
| 2 | `refactor/memory-p1` | P1-3 + P1-5 | 2-3 h |
| 3 | `refactor/memory-p2` | P2-6 + P2-7 | 2-3 h |
| 4 | `refactor/tier-s` | S1-S4 | 3-5 h |
| 5 | `refactor/tier-m` | M1 + M3 + 全件 pytest + bot 再起動 | 4-6 h |

---

## Phase 1: メモリ P0 (refactor/memory-p0)

### P0-1: 画像 base64 上限引き下げ
`services/claude_api/entry.py:109,111`
```python
_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 10 → 8 MB
_IMAGE_MAX_COUNT = 8  # 据え置き
```

### P0-2: `_channel_semas` evict バグ修正
`bot.py:152-165`

`list(_channel_semas.keys())` でスナップショットを取り、`over` 件 evict したら break する書き換え。locked sema は skip、自己 channel は skip。

### Phase 1 commit message
```
feat(memory): cap image base64 size + fix channel_semas LRU evict

- _IMAGE_MAX_BYTES: 10MB → 8MB (~16MB peak worst-case)
- _channel_semas evict: list(keys()) snapshot walk, skip locked

Per docs/2026-05-14-memory-safety-improvement-plan.md P0-1, P0-2.
```

### 検証
- `pytest tests/test_claude_api_entry.py tests/test_bot.py`
- 追加テスト: 500 個 locked sema → 501 個目要求で over 件 evict 動作確認

---

## Phase 2: メモリ P1 (refactor/memory-p1)

### P1-3: `_daily_quota` deque 全面移行
`bot.py:208-228`

`list[float]` → `deque[float]`、`popleft` で expired 削除。シグネチャは互換維持。

### P1-5: kanae_daily race 調査・対応
1. **調査 grep**:
   ```bash
   grep -n "state_dir\|kanae_emotion\|kanae_daily\|kanae_summary" \
        scripts/kanae_daily.py services/emotion/persistent_session.py \
        services/emotion/feedback.py services/emotion/daily.py
   ```
2. 競合あり → `services/emotion/lock.py::emotion_lock` 相当を適用
3. 競合なし → スキップ

### Phase 2 commit message
```
feat(memory): use deque for daily quota + [optional] kanae_daily lock

- _daily_quota: list → deque, popleft expired entries in O(1)
- [if applicable] add file lock to kanae_daily state writes

Per docs/2026-05-14-memory-safety-improvement-plan.md P1-3, P1-5.
```

### 検証
- `pytest tests/test_bot.py` (quota 関連)
- lock 追加時は `pytest tests/test_emotion_*.py`

---

## Phase 3: メモリ P2 (refactor/memory-p2)

### P2-6: vscode_sync `_buffers` LRU 化
`services/vscode_sync/aggregator.py:84`

- `_buffers` → `OrderedDict`, `MAX_BUFFERS=100`
- `_sessions_seen` → `deque(maxlen=1000)` + 補助 set

### P2-7: ingest_channel_history ストリーム化
`scripts/ingest_channel_history.py:40-87`

`deque(maxlen=10)` window で on-the-fly ペア発見 + yield。

### Phase 3 commit message
```
perf(memory): bound vscode_sync buffers + stream ingest history

- _buffers: OrderedDict + LRU evict at MAX_BUFFERS=100
- _sessions_seen: deque(maxlen=1000) + helper set
- ingest_channel_history: deque(maxlen=10) sliding window

Per docs/2026-05-14-memory-safety-improvement-plan.md P2-6, P2-7.
```

### 検証
- `pytest tests/test_vscode_sync*.py` (あれば)
- 手動: `scripts/ingest_channel_history.py --dry-run` で挙動同値性

---

## Phase 4: リファクタ Tier S (refactor/tier-s)

### S1: `utils/atomic_write.py` 新規
```python
def atomic_write_json(path, data, *, mode=0o600, indent=2): ...
def atomic_write_text(path, text, *, mode=0o600, encoding="utf-8"): ...
def atomic_write_bytes(path, data, *, mode=0o600): ...
```
共通実装: uuid tmp → `os.replace`

**置換対象**:
- `services/session.py:119-134`
- `services/emotion/state.py:174-181`
- `services/emotion/daily.py:78`

### S2: `services/mcp_selector.py` 新規
- `pick_mcp_servers`, `get_mcp_servers_by_keys`, `_KEYWORDS_*`, `_DOCS_TRIGGERS` を移動
- **`MCP_SERVERS` 辞書は config.py に残す**
- mcp_selector.py 内で `from config import MCP_SERVERS`

**import 書き換え**: `services/claude_api/entry.py`, `services/claude_sdk.py`

### S3: `utils/cache.py` 新規
- `TTLLRUCache[K, V]` (asyncio-only、ロックなし)
- `_thread_context_cache` のみ置換 (regex cache は不変なので据え置き)

### S4: `utils/image_validator.py` 新規
```python
def validate_image_bytes(data: bytes, max_bytes: int = 8 * 1024 * 1024) \
    -> tuple[str | None, str]:
    """Returns (mime, error_msg). mime is None when invalid."""
```
magic byte + MIME + size を一括検証。

**利用箇所**: `utils/attachments.py`, `services/claude_api/entry.py` (Phase 5 M3 で更に統合)

### Phase 4 commit message
```
refactor(tier-s): extract atomic_write, cache, image_validator, mcp_selector

- utils/atomic_write.py: atomic_write_{json,text,bytes}
- utils/cache.py: TTLLRUCache (asyncio-only)
- utils/image_validator.py: validate_image_bytes (one-shot check)
- services/mcp_selector.py: pick/get MCP server logic, MCP_SERVERS stays in config

Per docs/2026-05-14-refactor-priorities-plan.md Tier S.
```

### 検証
- 新規 4 モジュールに単体テスト追加 (`tests/test_atomic_write.py` 等)
- `pytest tests/test_session.py tests/test_emotion_state.py tests/test_emotion_daily.py tests/test_claude_api_entry.py tests/test_attachments.py tests/test_discord_helpers.py` pass

---

## Phase 5: リファクタ Tier M (refactor/tier-m)

### M1: emotion runtime context オブジェクト化 (完全移行)

`services/emotion/runtime.py`:
```python
class EmotionDaemonContext:
    def __init__(self): self._daemon = None
    def set(self, daemon): self._daemon = daemon
    def get(self): return self._daemon
    def is_available(self): return self._daemon is not None and self._daemon.is_alive

EMOTION_CTX = EmotionDaemonContext()
```

**書き換え**:
- `bot.py`: `set_emotion_daemon(daemon)` → `EMOTION_CTX.set(daemon)` (2 箇所)
- `bot.py`: `_emotion_daemon` global → `EMOTION_CTX.get()`
- `services/emotion/feedback.py`: `get_emotion_daemon()` → `EMOTION_CTX.get()`
- silence tick も同様に書き換え

### M3: `entry.py` 分解
- `services/claude_api/image_processor.py` 新規 — `_extract_attached_image_blocks`, `_safe_image_path` 移動。内部で `utils.image_validator` 利用
- `services/claude_api/client_factory.py` 新規 — `_make_api_client` 移動

`entry.py` 488 行 → ~300 行を目標

### Phase 5 commit message
```
refactor(tier-m): EmotionDaemonContext + extract image_processor & client_factory

- services/emotion/runtime.py: EmotionDaemonContext (no compat wrapper)
- services/claude_api/image_processor.py: image base64 + validation
- services/claude_api/client_factory.py: API client creation

Per docs/2026-05-14-refactor-priorities-plan.md M1, M3.
```

### 最終検証
1. `pytest tests/` 全件 pass
2. `python3 -c "import bot; import web_api"` で import 整合性
3. bot.py 停止 → 再起動 → on_ready ログ + Discord で 1 メッセージ送信応答確認
4. 24h RSS / log 監視

---

## PR テンプレート

各 Phase の PR は以下のフォーマット:

```markdown
## Summary
- {Phase N の主要変更 3 行}

## Changes
- {ファイル別の変更点}

## Test plan
- [ ] pytest {関連テストファイル}
- [ ] (Phase 5 のみ) pytest tests/ 全件
- [ ] (Phase 5 のみ) bot 再起動 + Discord 動作確認

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

---

## ロールバック戦略

| 状況 | 対応 |
|------|------|
| 単一 Phase で問題発覚 | 該当 PR を `git revert <merge-commit>` |
| 24h RSS 悪化 | Phase 1 (画像上限) を revert |
| bot 起動失敗 | `scripts/bot-rollback.sh` 利用 |

---

## 進捗チェックリスト

- [ ] Phase 1: P0-1 + P0-2 → PR マージ
- [ ] Phase 2: P1-3 + P1-5 → PR マージ
- [ ] Phase 3: P2-6 + P2-7 → PR マージ
- [ ] Phase 4: S1 + S2 + S3 + S4 → PR マージ
- [ ] Phase 5: M1 + M3 → 全件 pytest → bot 再起動 → PR マージ
- [ ] 24h 後の RSS 確認

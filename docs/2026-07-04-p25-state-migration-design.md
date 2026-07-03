# P2.5 状態移行 — concurrency 設計メモ

計画レビュー（gpt-5.5-pro）の必須要件「module global 28個の移行は concurrency model の
変更。着手前に lock 粒度・lock 順序・await 中の lock 保持禁止・cancellation 時の
release・semaphore 二重解放防止・session/mem0 書き込みの exactly-once を明文化」に
対応する1枚。

## bot.py 側の状態16個の群分け（結合度基準）

### 群① RateLimitState（相互参照あり: クールダウン判定とクォータ消費が同一ゲート）
| global | 型 | 並行性の現状 |
|---|---|---|
| `cooldowns` | OrderedDict[user_id, float] | await を跨がない同期区間でのみ read/write → lock 不要 |
| `_daily_quota` | OrderedDict[user_id, deque] | 同上 |

- class 化: `RateLimitState`。メソッドは全て**同期**（await しない）に保つことで
  lock フリーを維持する。async 化したくなったら設計に戻る
- `services/rate_limit.py`（共有台帳）が既に一部を持つ — 移行時は bot.py 側の
  フォールバック実装を削除し共有台帳へ一本化（defensive import の解消）

### 群② OverrideState（/model /effort と読み出しのみ）
| global | 型 | 並行性の現状 |
|---|---|---|
| `_MODEL_OVERRIDES` | dict[(ch,user), str] | 単一 dict 代入/参照のみ（GIL 原子性で足りる） |
| `_EFFORT_OVERRIDES` | dict[(ch,user), str] | 同上 |
| `sessions` | dict[ch, SessionInfo] | services/session.py の store と二重管理気味 → **P4a で統合してから移行**（順序依存） |

### 群③ RuntimeGuards（起動時の一回きりフラグ + リソース）
| global | 型 | 並行性の現状 |
|---|---|---|
| `_tree_synced` / `_cleanup_scheduled` / `_vscode_sync_started` | bool | on_ready 再入ガード。同期区間のみ |
| `_http_session` | ClientSession \| None | 生成は一回、close は shutdown |
| `_background_tasks` | set[Task] | `_track_task` 経由のみで touch |
| `_channel_semas` | OrderedDict[ch, Semaphore] | **要注意**: LRU eviction が「使用中の semaphore」を落とすと二重取得が可能になる。移行時に「acquire 中は evict 不可」の参照カウントを付けるか、eviction を廃止して上限で拒否する |

## ルール（全群共通）

1. **lock は「await を跨ぐ read-modify-write」にのみ導入**。同期区間で完結する dict 操作
   に lock を足さない（新たな deadlock 面を作らない）
2. **lock 順序**: 群をまたいで同時に持たない（そもそも持つ必要がある設計にしない）
3. **await 中の lock 保持禁止**: state class のメソッドは「同期で読む→await→同期で書く」
   の形にし、書き戻し時に前提が変わり得る値は compare-and-set 的に再検証
4. **cancellation**: semaphore の release は必ず `try/finally`（取得直後に finally 圏内へ
   入る acquired フラグ方式 — バックログ P0 のセマフォ解放漏れ修正と同じイディオム）
5. **exactly-once**: session append / mem0 record の呼び出しは on_message の finally
   ではなく成功パスにのみ置く（リトライは claude_api 層の責務で、bot 層は再送しない）
6. **AppContext は配線専用**: 群 class への参照を持つだけ。ロジック・状態を直接持たせ
   ない（新 god object 化の防止）。`reset()` / `close()` を各群に実装し conftest から
   確実に初期化
7. 移行期の旧 global は「新 state への委譲 property」にして段階置換。**新しい module
   global の追加は禁止**（ruff で機械 enforcement できないため PR レビュー項目に）

## 移行順序

1. 群③ RuntimeGuards（最小リスク: 起動時のみ）→ 24h 観察
2. 群① RateLimitState（判定ロジックは同期のまま移す）→ 24h 観察
3. 群② OverrideState — ただし `sessions` は **P4a（セッション統合）完了後**

## claude_api 側の12個

session_store（TTL dict×2）は P4a で session.py と統合。ratelimit._state singleton は
entry スコープの受け渡しに変更（P3 の entry 分解と同時）。compaction のカウンタ類・
tool_catalog の bool・_cli_runner の AvailabilityCache は「プロセス寿命キャッシュ」で
並行害が無いため現状維持（クラス化の費用対効果なし — 全 global 撲滅が目的ではない）。

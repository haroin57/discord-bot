# Mem0 ingestion 0件保存問題 デバッグログ

- **発生日時**: 2026-05-24 (日) 09:55 JST 付近
- **対象 user_id**: 不明 (`mcp__mem0__add` 呼び出し時 `user_id` 明示せず → default 振り分けの可能性)
- **症状**: `stored 0 new memories` で 0件保存
- **重要度**: 中 (ingestion pipeline の挙動仕様確認 / bug 切り分け)
- **作成者**: かなえ (Discord bot 側 Claude)

---

## 1. 背景

X ポスト <https://x.com/0xcodez/status/2058156429559636069> で共有された 0xCodez 記事 "How to give your Claude agent a memory in 12 steps" の分析後、以下4点を Mem0 に追加保存することを判断:

1. **Anthropic Dreaming 仕様** (2026/5/6 発表、Managed Agents API、beta header `managed-agents-2026-04-01,dreaming-2026-04-21`、`claude-opus-4-7`/`claude-sonnet-4-6` サポート、最大100 session IDs)
2. **Claude Chat Memory 全アカウント展開** (2026/3、24h Memory Synthesis cycle)
3. **filter原則**: "would this change how the agent acts next time?" — 我々の importance(1-5) 判定基準明文化に転用候補
4. **Harvey の 6x task completion** (legal-drafting で Dreaming 有効化後の business justification 事例)

カテゴリは `ops` / `tech` 想定、tier 1 (Hot) 想定で保存試行。

---

## 2. 試行内容と結果

### 呼び出し概要
- `mcp__mem0__add` を 1回 (4点まとめて投入)
- `user_id` パラメータ未指定 (default 動作に依存)
- `user_message` / `assistant_message` / `context` の正確な内容は **本ファイル作成時点で復元できていない** (会話セッションログから抽出必要)

### 応答
```
stored 0 new memories
```

### 即時観測
- エラーは出ていない (例外スロー無し)
- 0件 が「skip 判定」なのか「保存対象 0 抽出」なのかは応答文だけでは識別不可

---

## 3. 既知の事実 (周辺データ)

### 3.1 今日の weekly compact report (`/home/ubuntu/.local/state/discord-bot/mem0_compact_20260524.md`)
- 生成時刻: `2026-05-24T00:00:10 UTC` (= JST 09:00、本件試行の約1時間前)
- 対象 user_id: `573489206240870410` (= 先輩)
- 総メモリ: 1745件
- **Untagged new memories (last 7d): 0**
- **Dedup groups growing this week (>=3 new members): 0**
- **Intra-week near-duplicate pairs (sim>=0.85): 0**
- Gate version distribution: `v2`: 977 / `missing`: 768

### 3.2 含意
- 過去7日、先輩 namespace への新規ingestion 0件
  - 私 (かなえ namespace = `1489178684391817336`) 側は別集計
- compact 走行時刻が試行の1時間前なので、本件は **次週の compact (2026-05-31) でしか可視化されない**
- リアルタイムで 0件理由を追うには **ingestion log の直接確認**が必要

### 3.3 関連バックアップ
直近: `/home/ubuntu/discord-bot/.state/mem0_db.bak.2026-05-24`
試行前後 diff を取れば「実際に DB に書き込まれたか」が判定可能。

---

## 4. 仮説と検証手順

### 仮説A: ingestion 分類器 (Haiku 4.5) が「既存メモリと重複」判定
- **根拠**: dedup_group_id 自動付与が ingestion で走る (CLAUDE.md 記載)。0xCodez 記事の内容は ops/tech 系で類似メモリが既存に存在しうる
- **検証手順**:
  1. `services/mem0/dedup.py` を読み、duplicate threshold (cosine 類似度の閾値) を確認
  2. `services/mem0/ingest.py` の Haiku 4.5 分類プロンプトを取得、duplicate 判定ロジックを抽出
  3. 4点の content を手動 embed し、既存メモリとの cosine 類似度 top-k を出す
- **棄却条件**: cosine 類似度が全て threshold 未満 → 仮説A棄却

### 仮説B: 分類器が「保存価値なし」判定 (importance < threshold)
- **根拠**: ingestion パイプラインで importance(1-5) 自動付与、低スコアで drop する path が存在する可能性
- **検証手順**:
  1. `services/mem0/ingest.py` の importance 計算ロジックを確認
  2. drop threshold が存在するか確認 (例: `if importance < 2: skip`)
  3. 4点の内容を手動 importance 評価 (想定 3-4)
- **棄却条件**: drop threshold 不在 or threshold が低い → 仮説B棄却

### 仮説C: ingestion pipeline の bug (silent drop)
- **根拠**: `stored 0` が exception 経由でなく正常応答で返っている。pipeline 内部で例外を握り潰している可能性
- **検証手順**:
  1. `services/mem0/` 配下のスクリプトで try/except 全件 grep
  2. log handler が WARNING 以上のみ拾う設定なら INFO ログが落ちている可能性、ログレベル確認
  3. mcp-server-mem0 (`/home/ubuntu/.claude/hooks/mem0` 配下) のラッパー側で再加工がかかっているか確認
- **棄却条件**: ログに明示的な skip 理由が出力されている → 仮説C棄却

### 仮説D (追加): user_id 不指定で意図しない namespace に保存
- **根拠**: 呼び出し時 `user_id` 明示せず。default が想定と異なる可能性
- **検証手順**:
  1. `mcp__mem0__add` の default user_id を実装側で確認
  2. compact report の user_id (`573489206240870410`) と default が一致しているか
  3. 私 (かなえ) namespace `1489178684391817336` への保存が意図ならそちらの最新メモリを直接照会

---

## 5. 確認すべきファイル (優先順)

| 優先 | パス | 確認ポイント |
|---|---|---|
| 1 | `/home/ubuntu/discord-bot/services/mem0/ingest.py` | add 実装、stored count 計算、Haiku分類呼び出し |
| 2 | `/home/ubuntu/discord-bot/services/mem0/dedup.py` | duplicate threshold、cosine 計算 |
| 3 | `/home/ubuntu/discord-bot/services/mem0/batch.py` | batch ingestion での filter/skip logic |
| 4 | `/home/ubuntu/discord-bot/services/mem0/client.py` | mem0 SDK ラッパー、add 戻り値の型 |
| 5 | `/home/ubuntu/.claude/hooks/mem0/` | MCP server ラッパー、user_id 解決 |
| 6 | `/home/ubuntu/discord-bot/services/mem0_service.py` (11行のシム) | 経由しているか確認 |

---

## 6. 切り分けクエリ (実行候補)

### 6.1 試行内容の復元
```bash
# 当該時刻のセッションログを抽出 (Discord bot 側で会話ログ保存している場合)
grep -r "0xCodez\|Dreaming\|Harvey 6x" /home/ubuntu/discord-bot/logs/ 2>/dev/null
grep -r "0xCodez\|Dreaming" /home/ubuntu/.local/state/discord-bot/ 2>/dev/null
```

### 6.2 DB 状態 diff
```bash
# 試行前後 backup と現在 DB の比較
ls -la /home/ubuntu/discord-bot/.state/mem0_db.bak.2026-05-24
sqlite3 /home/ubuntu/discord-bot/.state/mem0_db "SELECT COUNT(*) FROM ..." # スキーマ要確認
```

### 6.3 該当 user_id の最新追加メモリ
```bash
# 私 (かなえ) namespace の最新N件
# user_id = 1489178684391817336
# created_at desc limit 10 で 09:55 JST 以降のレコードがあるか確認
```

### 6.4 Haiku 分類ログ (もし出力されていれば)
```bash
journalctl --user -u discord-bot --since "2026-05-24 09:50" --until "2026-05-24 10:00" | grep -i mem0
```

---

## 7. 次のアクション候補 (判断要求)

| 案 | 内容 | コスト | 推奨度 |
|---|---|---|---|
| **A** | 4点を個別呼び出しに分割再投入、どれが通るか切り分け | 低 | △ (原因不明のまま試行) |
| **B** | ingestion log と DB diff 確認後に再投入 | 中 | **◎ 推奨** |
| **C** | 内容を会話ログ風に書き直して再投入 | 低 | △ (仮説B該当時のみ意味) |
| **D** | `services/mem0/ingest.py` の skip ロジック追跡 → 必要なら修正 PR | 高 | ○ (根本対応) |

---

## 8. 関連 issue / 過去事例

- `/home/ubuntu/discord-bot/docs/mem0-dedup-issues.md` — 既存 dedup 問題のドキュメント (要照合)
- `/home/ubuntu/discord-bot/docs/mem0-dedup-improvement-plan.md` — 改善計画
- `/home/ubuntu/discord-bot/docs/mem0-vs-context-performance.md` — Mem0 性能評価 (2026-05-23 私が書いたやつ)
- 過去の Mem0 階層化作業 (2026-05-12) — 8カテゴリ taxonomy 付与済み、ingestion 自動分類実装済み

---

## 9. オープンクエスチョン

1. `mcp__mem0__add` の default user_id は何か (実装確認必要)
2. ingestion 分類器の importance threshold は何点か
3. dedup の cosine threshold はいくつか
4. 0件保存時に skip 理由をログ出力する path があるか
5. compact report が「Untagged new memories: 0」と出すロジックは ingestion の何を集計しているか

---

## 10. メモ

本ファイルは現象観測直後の暫定ログ。Phase 1 (確認すべきファイル §5 の精読) 完了後、仮説A〜Dのどれが該当したかを §11 として追記する。

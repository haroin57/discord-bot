# 2026-05-14 Mem0 大改修 実装サマリ

**ステータス**: 4 Phase 全完了、本番投入済 (bot active)
**設計 docs**: [quality-cleanup-plan](2026-05-14-mem0-quality-cleanup-plan.md), [hebbian-reinforcement-plan](2026-05-14-mem0-hebbian-reinforcement-plan.md), [timestamp-backfill-plan](2026-05-14-mem0-timestamp-backfill-plan.md), [obsidian-graph-improvement](2026-05-14-mem0-obsidian-graph-improvement.md)

---

## 0. 全体結果

| Phase | 内容 | Before | After |
|---|---|---|---|
| 1 | ノイズ・一時状態 fact の削除 | 1527 件 | 1382 件 (-145) |
| 2 | Hebbian 重複統合 (重み集約 + 削除) | 1382 件 | 1126 件 (-256) |
| 3 | Discord メッセージから created_at 逆引き | created_at 持ち: ~147 件 | **420 件** (+273) |
| 4 | Obsidian-style entity graph layer 新設 | (なし) | entities=**844**, edges=**1322**, aliases=**1099** |

**Mem0 fact 総数**: 1527 → 1126 (26% 圧縮)
**fact 品質**: ノイズ除去 + 重複集約で signal-to-noise が大幅改善
**新検索能力**: entity graph 1-hop expansion で「Rust 関連の話全部」のような broad query が網羅可能に
**テスト**: **135 件全 pass** (mem0 関連)

---

## 1. Phase 1: 品質 cleanup (ノイズ削除)

### 設計
全 fact を Sonnet で 6 クラス (`noise / transient / preference / decision / knowledge / episodic`) に分類、`noise / transient` (confidence ≥ 0.70) を物理削除。残った fact には `value_class` と `value_confidence` を metadata に追記。

### 実装
- 新規: [scripts/mem0_value_cleanup.py](../scripts/mem0_value_cleanup.py)
- prompt 定義 + 6-class enum (`VALUE_CLASSES`, `DELETE_CLASSES`)
- `mem.update(memory_id, data, metadata)` で metadata 部分更新 (`mem.update()` は data 引数必須・metadata 部分更新型)
- max_concurrent + delay でレート制御、bot 並走可

### 結果
| ユーザー | total | deleted | kept |
|---|---|---|---|
| 先輩 (...0410) | 1467 | **150** (10.2%) | 1304 |
| お父さん (...7522) | 99 | 25 (25%) | 74 |
| 別 | 7 | 1 | 6 |
| **GRAND TOTAL** | 1573 | **176** | 1384 |

分布 (kept): preference=602, episodic=413, knowledge=211, decision=156

### サンプル削除
- `[noise 0.97]` "Restarted/Rebooted something"
- `[transient 0.97]` "現在のRAM使用量は2.2GB、空き容量は1.6GB"
- `[transient 0.95]` "Swapは2.7GB使用している"
- `[transient 0.80]` "プルリクエストを出す予定"

---

## 2. Phase 2: Hebbian 強化

### 設計
**重複は drop せず、既存 fact の重みを増やす**シナプス強化モデル。Cognee の weighted graph、ACT-R の base-level activation、Zep の episode-mentions reranker と同思想。

検出ルート 2 つ:
1. **新規 ingest 時**: `check_near_dup` が重複検出したら `reinforce_existing()` で既存 fact の `mention_count++` (リアルタイム)
2. **遡及**: 既存 1126 件をペアワイズ判定 → cluster → keep に集約 (mass reinforcement)

### Metadata 拡張
| フィールド | 型 | 役割 |
|---|---|---|
| `mention_count` | int | 言及回数 (初期値 1) |
| `reinforced_at` | list[int] | 直近 10 件の reinforced unix 時刻 |
| `last_reinforced_at` | int | 最終 reinforced 時刻 |
| `importance` | int 1-5 | reinforced で +1 (cap=5) |

### Search rerank の boost
[services/mem0/search.py:_rerank_memories](../services/mem0/search.py#L126-L150) で LLM rerank の前に加算:
```python
boost_freq    = α_mention * log(mention_count)        # α=0.10, mention=1 → 0
boost_recency = α_recency * exp(-days_since / 30)     # α=0.05, half-life 30d
score += boost_freq + boost_recency
```
よく言及される & 最近 reinforced された fact が candidate 上位に来る。

### 実装
- 新規: [services/mem0/reinforce.py](../services/mem0/reinforce.py) — `reinforce_existing_sync()` + `reinforcement_boost()`
- 新規: [scripts/mem0_reinforce_existing.py](../scripts/mem0_reinforce_existing.py) — 既存 fact への遡及集約
- 編集: [services/mem0/dedup.py:check_near_dup](../services/mem0/dedup.py) — 戻り値を `(is_dup, reason, existing_id)` 3-tuple に
- 編集: [services/mem0/ingest.py](../services/mem0/ingest.py) — `is_dup=True` 時に `reinforce_existing()` 並列呼出
- 編集: [services/mem0/search.py:_rerank_memories](../services/mem0/search.py) — boost 加算
- 新規: [tests/test_mem0_reinforce.py](../tests/test_mem0_reinforce.py) (17 件)

### 結果 (遡及実行)
| | 値 |
|---|---|
| candidate pairs (cosine ≥ 0.80) | 497 |
| LLM 同一事実認定 | 376 |
| non-trivial clusters | 120 |
| keep facts updated | 120 |
| **duplicate facts deleted** | **256** |
| errors | 0 |
| Mem0 DB | 1382 → **1126** |

---

## 3. Phase 3: timestamp backfill

### 設計
既存 fact の大半は created_at メタが無い。Discord 元メッセージから vector マッチで逆引きして付与:
1. Discord channel.history() で全 user message 取得 (1496 件中 791 件 = 先輩発話)
2. OpenAI text-embedding-3-small で msg と fact 両方を vectorize
3. cosine 最近傍 (threshold ≥ 0.55) で対応付け
4. `mem.update()` で metadata に `created_at_unix` / `created_at` / `created_at_source` / `created_at_score` を追記

### 実装
- 新規: [scripts/backfill_mem0_timestamps.py](../scripts/backfill_mem0_timestamps.py)

### 結果
- facts without `created_at_unix`: 1046 件
- **matched (score ≥ 0.55): 420 件 (40.2%)**
- below threshold: 626 件 (リアルタイム会話分など、Discord ログに紐付かない fact)
- errors: 0

### サンプル (high score)
- `0.758` fact="Discord botに管理者権限を付与してほしいと要望した" ↔ msg="Discord bot内のAdmin権限を..."
- `0.706` fact="ashitabo_main をクローンした" ↔ msg="ashitabo_main/pull/24"
- `0.627` fact="Uses Mem0 for long-term memory storage" ↔ msg="これってMem0から取得はしてる？"

---

## 4. Phase 4: Obsidian-graph layer

### 設計
ChromaDB の vector search だけでは「同一 entity の異なる事実」の集約が弱い。Obsidian の `[[wikilink]]` + MetadataCache に倣い、**fact ⟷ entity を sqlite 別管理**して 1-hop 双方向ルックアップを可能に。

### Architecture
```
[Vector Layer]  ChromaDB (kanae_memories, 1126 facts)
                ↑ search top-K via cosine
[Graph Layer]   .state/mem0_graph.sqlite (新規, 65KB)
                ├─ entities         (id, canonical_name, type, user_id, mention_count)
                ├─ entity_aliases   (entity_id, alias)
                └─ fact_entity_edges (fact_id, entity_id, role, created_at)
[Ingest Hook]   extract.py が "entities" を出力 → ingest.py が graph upsert
[Search Hook]   search.py が top-3 candidate の entities → sibling facts を 1-hop expansion
```

### 実装
- 新規: [services/mem0/graph.py](../services/mem0/graph.py) (210行)
  - `upsert_entity`, `find_entity` (alias + fuzzy), `add_edge`, `get_related_facts`, `get_fact_entities`, `delete_fact_edges`, `stats`
  - SQLite WAL モード、`_db_lock` で書込シリアライズ
- 編集: [services/mem0/extract.py:_EXTRACT_PROMPT](../services/mem0/extract.py) — `entities` フィールド追加 (name/type/aliases、type は person/project/tech/media/place/concept)
- 編集: [services/mem0/ingest.py](../services/mem0/ingest.py) — fact 保存後に `graph.upsert_entity` + `graph.add_edge` 自動呼出
- 編集: [services/mem0/search.py:search_memories_smart](../services/mem0/search.py) — 新規 `_graph_expand_candidates()` で 1-hop expansion
- 新規: [scripts/backfill_entities.py](../scripts/backfill_entities.py) — 既存 fact への遡及 entity 抽出
- 新規: [tests/test_mem0_graph.py](../tests/test_mem0_graph.py) (17 件)

### 結果 (backfill)
| | facts | entities | edges | skipped (no_entity) |
|---|---|---|---|---|
| 先輩 | 1046 | 1224 | 1224 | 241 |
| お父さん | 74 | 90 | 90 | 12 |
| 別 | 6 | 8 | 8 | 0 |
| **graph DB 最終** | — | **844** | **1322** | — |

### Top entities (mention_count 順)
```
Rust(tech)            32
Discord(tech)         24
LLM(tech)             21
ariiol(person)        19
TypeScript(tech)      17
Vercel(tech)          13
量子コンピュータ      12
Namitape(project)      9
みのミュージック       8
TOEIC                  7
沙耶の唄(media)        7
```

---

## 5. 新しい Search パイプライン (4段)

`services.mem0.search.search_memories_smart(query, user_id, limit)` の処理順:

```
1. _rewrite_query     — query を canonical キーワード 3-5 個に変換 (Sonnet)
2. search_memories_multi — ChromaDB cosine top-20 を rewrite 全 query で並列取得 + dedup
3. _graph_expand_candidates ← NEW
   top-3 候補の entities を sqlite で取得 → 各 entity の sibling facts (1-hop) を merge
4. _rerank_memories
   ├─ reinforcement_boost (mention_count + recency) を score 加算 + 並べ替え ← NEW
   └─ Sonnet LLM rerank で top-K を選抜
```

各段すべて [services/mem0/cli_compat.py](../services/mem0/cli_compat.py) 経由で CLI body 互換、overage 経路で課金。

---

## 6. metadata schema (新フィールド一覧)

すべて metadata に追加。既存 reader は `.get(key, default)` で安全に無視できる。

| フィールド | 由来 | 用途 |
|---|---|---|
| `value_class` | Phase 1 cleanup | preference/decision/knowledge/episodic 等の質的ラベル |
| `value_confidence` | Phase 1 | 分類信頼度 (0.0-1.0) |
| `mention_count` | Phase 2 | 強化された言及回数 |
| `reinforced_at` | Phase 2 | 直近 10 件の reinforced 時刻 |
| `last_reinforced_at` | Phase 2 | 最終 reinforced 時刻 |
| `importance` | Phase 2 (動作変更) | reinforced で +1 (cap=5) |
| `created_at_unix` | Phase 3 / ingest_channel_history | 元会話の unix 時刻 |
| `created_at` | Phase 3 | ISO 文字列 |
| `created_at_source` | Phase 3 | `vector_match` / `history_ingest` |
| `created_at_score` | Phase 3 | vector マッチ時の cosine |

---

## 7. 既存依存への影響

(詳細は [作業中の調査結果](#) 参照)

| 影響 | 対応 |
|---|---|
| `check_near_dup` 2→3-tuple | 既存呼出は ingest.py 1 箇所のみ、修正済 |
| metadata 新フィールド | 既存 reader 全て `.get(key, default)` で安全 |
| `mem.update()` 仕様 | 当初「全置換型」と誤認、実は「部分更新型 (data 引数必須)」と判明し reinforce.py / cleanup script を修正 |
| `importance` reinforced で +1 | weekly_mem0_compact.py の archive 判定に `mention_count < 4` 条件を追加して redundant な保護 |
| MCP tool docstring | search / list_all に新フィールド説明追記 |
| ChromaDB direct write | なし (Mem0 SDK 経由のみ) |
| 外部 consumer | MCP / web_api 経由で新フィールド露出するが read-only |

---

## 8. ロールバック資産

問題発生時に巻き戻せるよう各 phase 前に物理コピー:
- `.state/mem0_db.bak-cleanup-20260514-1810`     (Phase 1 前)
- `.state/mem0_db.bak-reinforce-20260514-1838`   (Phase 2 前)
- `.state/mem0_db.bak-backfill-20260514-1845`    (Phase 3 前)

復旧手順:
```bash
sudo systemctl stop discord-bot
rm -rf .state/mem0_db
mv .state/mem0_db.bak-<phase>-<ts> .state/mem0_db
sudo systemctl start discord-bot
```

`.state/mem0_graph.sqlite` は Phase 4 で新規作成。捨てれば pre-Phase 4 状態に戻る (entity expansion が無効化されるだけ、本体 fact は無事)。

---

## 9. テスト状況

```
tests/test_mem0_reinforce.py        17 件 (新規)
tests/test_mem0_graph.py            17 件 (新規)
tests/test_mem0_extract_format.py   既存 (CLI compat 移行で更新済)
tests/test_mem0_ingest.py           既存 (dedup 3-tuple 対応)
tests/test_mem0_search.py           既存
─────────────────────────────────
合計                                135 件 全 pass
```

---

## 10. 運用観察ポイント

これから 1-2 週間運用しながら以下を観察:

1. `mention_count` 分布 — 平均 / max / 1 件のみの fact 比率
2. Mem0 ingest ログ `Mem0 gate: facts=N kept=N skip(dup=N)` — dup の絶対数が以前より増えていれば「reinforce のおかげで個別 fact を増やさず重みに集約できている」証
3. graph_expand 発火頻度 — `services.mem0.search: graph_expand: added N sibling facts` ログを bot.log で確認
4. bot の応答品質 — 「先輩がよく話す topic」の被 hit が改善してるか体感
5. weekly_mem0_compact の archive 判定 — `mention_count ≥ 4` の fact が誤って archive 対象になっていないか

---

## 11. 次のステップ候補 (今日は未着手)

[embedder migration plan](2026-05-14-mem0-embedder-migration.md) — `text-embedding-3-small` → `text-embedding-3-large` への切替。Mem0 DB 全リビルドが必要なので、Phase 1-4 の効果が出尽くしてから判断。

---

## 12. 工数記録

| Phase | 設計 | 実装 + テスト | dry-run + 本番 | 合計 |
|---|---|---|---|---|
| 1 | docs (前段) | 約 20 分 | dry-run 約 4 分 + 本番 約 20 分 | 約 45 分 |
| 2 | docs | 約 30 分 | dry-run 約 2 分 + 本番 約 4 分 | 約 40 分 |
| 3 | docs | 約 20 分 | dry-run 約 1 分 + 本番 約 3 分 | 約 25 分 |
| 4 | docs | 約 60 分 (graph layer + extract + search + tests) | backfill 約 13 分 | 約 75 分 |
| **合計** | — | — | — | **約 3.5 時間** |

すべて bot active のまま (Phase 2 と 3 は短時間 bot 停止) で実施。先輩との Discord 会話は中断ゼロで完了。

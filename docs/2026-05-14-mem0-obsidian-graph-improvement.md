# Obsidian 風 Knowledge-Graph Layer による Mem0 検索品質改善

## Context

Mem0 の現状は **ChromaDB 単層** の vector search で、cosine 類似度の弱点 (表記揺れ、同一 entity の異なる事実、複合トピック) が検索品質を制約している。Obsidian の `[[wikilink]] + MetadataCache + hierarchical tag + typed frontmatter` の組み合わせは、まさにこの弱点を補う設計パターンとして実証されている (10年以上、数百万ユーザでの運用実績)。

学術的にも Zep/Graphiti (vector + temporal graph) が MemGPT 比 +18.5% の retrieval 精度を出していて、**ハイブリッド (vector + graph)** が現代の AI 記憶システムのデファクト。

期待される結果:
- 「Rust 関連の話全部」のような broad query が graph 1-hop expansion で網羅される
- 「ナミテープ」「Namitape」のような表記揺れが entity resolution で吸収される
- 「先月の」「最近の」のような temporal query が直接フィルタできる
- 既存の vector search はそのまま、上に layer を被せる形なので段階導入可能

## 調査結果 (Phase 1 完了済)

### Obsidian の核設計
- **wikilink `[[]]`**: ファイル名で resolve、alias は未対応 (実装制限)、解決優先順位はファイル名 > path > 正規化
- **MetadataCache**: `resolvedLinks` / `unresolvedLinks` で backlink を index、リアルタイム更新
- **typed Properties** (YAML frontmatter): 7 種類 (Text/Number/Date/Checkbox/List/Links/Date+Time)、vault 全体で型一貫性
- **hierarchical tag `#a/b/c`**: `/` 区切り、prefix match で範囲検索
- **Dataview/Bases**: SQL-like query, `WHERE`, `SORT`, `FROM` operators
- **Daily Notes**: 時系列ノートで週次集約 (Templater で自動化)

### Knowledge-graph memory の事例
- **Letta (旧 MemGPT)**: Core/Recall/Archival の 3 層 ([letta.com](https://www.letta.com/blog/agent-memory))
- **Zep + Graphiti**: 双時間モデル (event time + ingestion time)、edge ごとに valid period、MemGPT 比 +18.5% accuracy ([arxiv 2501.13956](https://arxiv.org/abs/2501.13956))
- **Mem0 v1.1 の graph 機能は廃止** (2026): 4000 行削除、現在は埋め込み entity リンク方式に回帰 → **Mem0 SDK の graph_store 機能には頼らず、自前で実装した方が安全**
- **HybridRAG**: vector + graph traversal が標準パターン

### 既存 Mem0 コード
- mem0 SDK の `graph_memory.py` (744行、Neo4j ベース) は存在するが **未使用** (config に `graph_store` なし) — そのまま放置で良い
- tags は flat 配列、entity 抽出ロジックなし
- search パイプライン: `_rewrite_query` → `search_memories_multi` (top-20) → `_rerank_memories` (top-5)
- **graph 挿入ポイント**: top-20 取得後 / rerank 前

---

## 設計: 4 層モデル

```
[L4 Search Pipeline]  search_memories_smart_v2
    └─ query parse (operators: entity:, tag:, since:)
    └─ vector retrieve top-K from ChromaDB
    └─ graph expansion (1-hop entity siblings)
    └─ temporal decay (created_at_unix 利用)
    └─ LLM rerank (Sonnet)

[L3 Graph Layer]  .state/mem0_graph.sqlite (新規)
    ├─ entities table        (canonical_name, type, aliases)
    ├─ fact_entity_edges     (fact_id ⟷ entity_id, role)
    └─ entity_aliases table  (表記揺れ resolution)

[L2 Vector Layer]  既存 ChromaDB (kanae_memories)
    └─ fact embeddings + metadata (category, tags, tier, importance, created_at_unix)

[L1 Ingest]  extract.py + cli_compat.py (既存) + 拡張
    └─ Sonnet 抽出: facts + entities (NER) を同時に
    └─ entity を Graph Layer に upsert、edge を貼る
```

### Obsidian → Mem0 マッピング

| Obsidian | Mem0 対応 | 実装 |
|---|---|---|
| Markdown ファイル | fact (ChromaDB row) | 既存 |
| YAML frontmatter | metadata (typed) | 既存 + entity_refs を追加 |
| `[[wikilink]]` | fact → entity edge | 新規 sqlite |
| MetadataCache (backlinks) | entity → 全 fact 逆引き | sqlite query |
| `#tag/sub/leaf` | tags の `/` 区切り階層 | extract prompt 変更 |
| Search `tag:`, `path:` | DSL operators (`entity:`, `tag:`, `since:`) | search.py 改修 |
| Daily Notes (時系列集約) | weekly summary fact | cron + summarize LLM |

---

## 実装フェーズ

### Phase 1: Entity 抽出 + Graph Layer (約 1 週)

#### 1A. Sonnet で entity を fact と同時抽出

[services/mem0/extract.py:_EXTRACT_PROMPT](../../services/mem0/extract.py) を改修して、fact 抽出と並行で entity を抜く:

```python
# 既存:
# {"facts": [{"speaker": "self", "text": "...", "importance": 3, ...}]}
# 改修後:
# {"facts": [{..., "entities": [{"name": "Rust", "type": "tech", "aliases": []}]}]}
```

`type` は 6 種類: `person | project | tech | media | place | concept`。`aliases` は表記揺れ吸収用 (空のことが多い)。

#### 1B. SQLite Graph Layer

新規 [services/mem0/graph.py](../../services/mem0/graph.py) + DB `.state/mem0_graph.sqlite`:

```sql
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,         -- person/project/tech/media/place/concept
    user_id TEXT NOT NULL,      -- per-user namespace
    created_at INTEGER,
    mention_count INTEGER DEFAULT 0
);
CREATE TABLE entity_aliases (
    entity_id INTEGER REFERENCES entities(id),
    alias TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
);
CREATE TABLE fact_entity_edges (
    fact_id TEXT NOT NULL,      -- ChromaDB memory id
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    role TEXT,                  -- "subject" / "object" / "context" (任意)
    PRIMARY KEY (fact_id, entity_id)
);
CREATE INDEX idx_edges_entity ON fact_entity_edges(entity_id);
CREATE INDEX idx_edges_fact   ON fact_entity_edges(fact_id);
```

`mem0/graph.py` API:
- `upsert_entity(name, type, user_id, aliases=[]) -> entity_id`
- `add_edge(fact_id, entity_id, role)`
- `find_entity(query, user_id, fuzzy=True)` — alias / fuzzy match
- `get_related_facts(entity_id, limit=20)` — 1-hop expansion
- `get_fact_entities(fact_id)` — fact の entity 一覧

#### 1C. Ingest 側の配線

[services/mem0/ingest.py](../../services/mem0/ingest.py) で fact 保存後に graph も更新:

```python
fact_id = mem.add(...)  # ChromaDB に保存
for ent in fact.get("entities", []):
    eid = graph.upsert_entity(ent["name"], ent["type"], user_id, ent.get("aliases", []))
    graph.add_edge(fact_id, eid, role=ent.get("role"))
```

### Phase 2: Hierarchical tags (約 2 日)

[services/mem0/extract.py:_EXTRACT_PROMPT](../../services/mem0/extract.py) で tags を `/` 区切り階層に。

```python
# 既存: tags: ["python", "asyncio"]
# 改修: tags: ["tech/lang/python", "tech/async/asyncio"]
```

検索側 ([services/mem0/search.py](../../services/mem0/search.py)) で prefix match:

```python
def filter_by_tag_prefix(memories, tag_prefix):
    return [m for m in memories if any(
        t.startswith(tag_prefix) for t in (m.get("metadata", {}).get("tags") or [])
    )]
```

### Phase 3: Search Pipeline v2 (約 3 日)

[services/mem0/search.py:search_memories_smart](../../services/mem0/search.py#L191) を改修:

```python
async def search_memories_smart_v2(query, user_id, *, limit=5):
    # 1) Query parse: operators 抽出
    parsed = parse_query(query)
    # parsed = {"text": "Rust の好み", "entity": "Rust", "tag_prefix": "tech/", "since": None}

    # 2) Vector retrieve
    candidates = await search_memories_multi(parsed["text"], user_id, limit=20)

    # 3) Graph expansion (1-hop)
    if parsed.get("entity"):
        entity = graph.find_entity(parsed["entity"], user_id, fuzzy=True)
        if entity:
            sibling_facts = graph.get_related_facts(entity.id, limit=10)
            candidates = merge_dedup(candidates, sibling_facts)
    else:
        # 上位 N の fact から entity を取って sibling を expansion
        for c in candidates[:5]:
            for e in graph.get_fact_entities(c.id):
                siblings = graph.get_related_facts(e, limit=3)
                candidates = merge_dedup(candidates, siblings)

    # 4) Filter by tag / since
    if parsed.get("tag_prefix"):
        candidates = filter_by_tag_prefix(candidates, parsed["tag_prefix"])
    if parsed.get("since"):
        candidates = [c for c in candidates
                      if c.metadata.get("created_at_unix", 0) >= parsed["since"]]

    # 5) Temporal decay: 新しい fact に bonus
    candidates = apply_temporal_bonus(candidates, half_life_days=30)

    # 6) LLM rerank (既存)
    return await _rerank_memories(parsed["text"], candidates, top_k=limit)
```

`parse_query` は Obsidian の search operator 構文を真似る:
- `entity:Rust query text` → entity フィルタ
- `tag:tech/* query text` → tag prefix
- `since:2026-04-01 query text` → 日付フィルタ

### Phase 4: 過去 fact への遡及 entity 抽出 (約 1 日 + 数十分の LLM 実行)

既存 1527 fact には entity 情報なし。Phase 1 完了後にスクリプトで遡及:

```bash
# scripts/backfill_entities.py (新規)
# 全 fact を読んで Sonnet で entity 抽出 → graph に upsert + edge
```

LLM 呼出 1527 回、各 1-2 sec → 約 40-60 分。CLI 互換化済なので overage 経路。コスト約 $3-5 (Sonnet)。

### Phase 5: Daily-Notes 風 weekly summary (約 2 日)

[scripts/weekly_mem0_compact.py](../../scripts/weekly_mem0_compact.py) は既に存在 (housekeeping)。これを拡張して **週次 summary fact** を生成:

```python
# 既存: 古い fact を tier=Cold に降格 / dup を統合
# 追加: 直近 7 日の fact (全 user, 全 category) を Sonnet に渡して
#   「今週の重要トピック上位 5」を要約 → 新しい fact として add (importance=4, tier=2, tags=["meta/weekly-summary"])
```

bot が「先週何話した?」と聞かれたら weekly summary fact が即 hit する。

### Phase 6: Search DSL を bot.py から expose (約 1 日)

bot が長期記憶を活用するための query rewriting。
[bot.py:690](../../bot.py#L690) の `search_memories_smart(raw_user_text, ...)` を `search_memories_smart_v2(raw_user_text, ...)` に切替。

Discord で先輩が `<@bot> entity:Rust 教えて` のような探索を直接できる UX も検討 (任意)。

---

## 修正対象ファイル

| ファイル | 変更内容 | Phase |
|---|---|---|
| 新規 [services/mem0/graph.py](../../services/mem0/graph.py) | SQLite graph layer (entities, edges, aliases) | 1B |
| 編集 [services/mem0/extract.py](../../services/mem0/extract.py) | extract prompt に entity 抽出を追加、tags 階層化 | 1A, 2 |
| 編集 [services/mem0/ingest.py](../../services/mem0/ingest.py) | fact 保存後の entity upsert + edge | 1C |
| 編集 [services/mem0/search.py](../../services/mem0/search.py) | search_memories_smart_v2 (graph expansion + DSL) | 3 |
| 新規 [scripts/backfill_entities.py](../../scripts/backfill_entities.py) | 既存 1527 fact への遡及 entity 抽出 | 4 |
| 編集 [scripts/weekly_mem0_compact.py](../../scripts/weekly_mem0_compact.py) | weekly summary fact 生成を追加 | 5 |
| 編集 [bot.py](../../bot.py) | search_memories_smart → v2 切替 | 6 |
| 新規 [tests/test_mem0_graph.py](../../tests/test_mem0_graph.py) | graph layer の単体テスト | 1B, 3 |

---

## 検証

### Phase 1 完了時
```bash
.venv/bin/python -m pytest tests/test_mem0_graph.py -v
# entity upsert / alias resolution / 1-hop expansion がすべて pass
```

### Phase 3 完了時 (graph expansion 効果測定)
```bash
# 同じ query で v1 と v2 を実行して結果比較
.venv/bin/python scripts/compare_search_v1_v2.py "Rust"
# v2 が v1 で取れない Rust 関連 fact を expand してきているか確認
```

### Phase 4 完了時
```bash
sqlite3 .state/mem0_graph.sqlite "SELECT count(*) FROM entities; SELECT count(*) FROM fact_entity_edges;"
# 期待: entities >= 100、edges >= 2000 (各 fact 平均 1-2 entities)
```

### Phase 5 完了時
```bash
# 翌週月曜の cron 後に weekly summary fact が生成されているか
.venv/bin/python -c "
from services.mem0_service import search_memories
import asyncio
r = asyncio.run(search_memories('先週', '573489206240870410', limit=3))
for x in r: print(x.get('memory', '')[:100])
"
# weekly-summary tag の fact が hit
```

### 統合検証 (Phase 6 完了時)
Discord で bot に「最近 Rust の話したっけ?」と話しかけて、過去複数ターンの Rust 関連 fact が context に流れることを確認。

---

## ロールバック条件

| トリガー | 対応 |
|---|---|
| Phase 1 で extract prompt 変更が既存 fact 抽出を壊す | extract prompt を Phase 1 前に戻す。entity 抽出だけ別 LLM call にする変形 |
| Phase 3 graph expansion で関係ない fact が混入し品質悪化 | search_memories_smart_v2 を v1 にフォールバックする env flag `MEM0_USE_GRAPH=false` を導入 |
| SQLite と ChromaDB の不整合 (fact 削除しても edge 残る等) | bot.py の Mem0 delete 経路でも `graph.delete_fact_edges()` を呼ぶ |

---

## オープン課題

1. **Mem0 SDK の graph_memory.py を使うか自前 sqlite か**: SDK は Neo4j 前提で重い + v1.1 で機能廃止の流れ → **自前 sqlite を推奨**
2. **entity 重複統合**: 「Rust」と「Rust 言語」を同じ entity にすべきか。canonical_name の正規化ルールが必要 (Sonnet に prompt で「最も general な name を返せ」と指示)
3. **temporal decay の half-life**: 30 日 / 90 日 / 365 日 のどれが体感に合うか実験要
4. **weekly summary の頻度**: 週次 / 隔週 / 月次 のどれにするか先輩判断
5. **既存 1527 fact の entity 抽出を embedding 移行前にやるか後か**: 移行前なら 3-small embedding 環境で抽出、移行後なら新環境で。移行と並走させる選択もあり

---

## 工数見積もり

| Phase | 工数 | 累積 |
|---|---|---|
| 1 (Entity + Graph) | 1 週 | 1 週 |
| 2 (階層 tags) | 2 日 | 1.3 週 |
| 3 (Search v2) | 3 日 | 1.7 週 |
| 4 (backfill) | 1 日 + 1 時間 LLM | 1.8 週 |
| 5 (weekly summary) | 2 日 | 2.1 週 |
| 6 (bot.py 切替) | 1 日 | **約 2.2 週** |

各 Phase 完了後に検証 → 問題なければ次へ進む段階導入。Phase 1+3 だけでも検索品質改善は実感できる構成。

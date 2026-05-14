# Mem0 重複記憶のシナプス強化 (Hebbian-style reinforcement) 実装計画

## Context

cleanup-plan (`docs/2026-05-14-mem0-quality-cleanup-plan.md`) の Phase 2 は「重複した fact を統合して delete する」設計だった。これを変更し、**重複検出時に既存記憶の重み (mention_count) を増やす** Hebbian-style reinforcement に切り替える。

理由:
- 同じ事実が複数回言及される = 先輩にとってその情報が重要である強い signal
- 単純 delete だと「言及頻度」の情報が捨てられる
- search rerank で `mention_count` を boost に使えば「先輩がよく話す topic」が上位に来る
- Cognee の weighted graph、ACT-R の base-level activation (recency + frequency) と同じ idea

期待される結果:
- 重複は drop されず、既存 fact の重みが増え続ける
- bot が「先輩が何度も言及してる事実」を優先的に引っ張れる
- 一過性の言及との重み差で priority 判定可能

調査出典:
- [Zep: episode-mentions reranker](https://arxiv.org/html/2501.13956v1)
- [Cognee: weighted graph nodes/edges](https://www.cognee.ai/blog/cognee-news/product-announcement-memory-weights)
- [ACT-R Base-Level Activation](https://pubmed.ncbi.nlm.nih.gov/30536740/)
- スコア式: `score × log(1 + α × weight)` (OpenSearch / Elasticsearch 標準パターン)

---

## Map — 現状 vs 改修後

### 現状の重複処理

```
extract → fact A 抽出
       → cosine 検索 (top 5)
       → check_near_dup(A, [existing_1..5])
           - cosine ≥ 0.95 → drop A
           - 0.80 ≤ cosine < 0.95 → LLM judge
                                       → 同一なら drop A
       → A keep / drop
```

drop されたら情報そのものが捨てられる。

### 改修後

```
extract → fact A 抽出
       → cosine 検索 (top 5)
       → check_near_dup(A, [existing_1..5])
           - hard_drop or LLM judged same
             → drop A, ただし existing fact の metadata を reinforce
                   mention_count += 1
                   reinforced_at.append(now_unix)
                   last_reinforced_at = now_unix
                   importance = min(importance + 0.5, 5.0)  # 上限あり
           - else → A keep
```

### 新規 metadata フィールド (既存 metadata に追加)

| フィールド | 型 | 初期値 | 更新タイミング |
|---|---|---|---|
| `mention_count` | int | 1 | 重複検出のたび +1 |
| `reinforced_at` | list[int] | [] | 重複検出時の unix 時刻を append (最新 10 件まで保持) |
| `last_reinforced_at` | int | null | 最新の reinforced unix 時刻 |
| `importance` | int (1-5) | 既存 | reinforced 時に上限 5 で +0.5 |

`importance` を使い続けるのは「既存ロジック (search filter / weekly compact tier 降格) との互換」のため。

---

## 設計

### Mem0 SDK の制約

`mem.update(memory_id, metadata=...)` は **metadata 全置換型** (調査済)。よって read-modify-write が必要:

```python
existing = mem.get(memory_id)             # 既存メタ取得
new_meta = {**existing["metadata"]}
new_meta["mention_count"] = existing["metadata"].get("mention_count", 1) + 1
new_meta["reinforced_at"] = (existing["metadata"].get("reinforced_at", []) + [int(time.time())])[-10:]
new_meta["last_reinforced_at"] = int(time.time())
new_meta["importance"] = min(int(existing["metadata"].get("importance", 3)) + 1, 5)
mem.update(memory_id=memory_id, metadata=new_meta)
```

並列 ingest 時の race condition は cleanup script で 1 度 normalize すれば実害なし (`mention_count` がたまに 1 個ズレるくらい)。bot は単一プロセスなのでほぼ問題ない。

### check_near_dup の改修

[services/mem0/dedup.py:check_near_dup](services/mem0/dedup.py#L61) の戻り値を `(is_dup, reason)` から **`(is_dup, reason, existing_id)`** に拡張:

```python
async def check_near_dup(
    candidate_text, candidate_category, near_results, *, dry_run=False
) -> tuple[bool, str, str | None]:
    for r in near_results:
        ...
        if score >= DEDUP_HARD_DROP:
            return (True, f"hard_drop({score:.3f})", r.get("id"))
        if score >= DEDUP_GRAY_LOW:
            if await llm_judge_same_fact(...):
                return (True, f"llm_judged_same({score:.3f})", r.get("id"))
    return (False, "novel", None)
```

### ingest.py の改修

[services/mem0/ingest.py:112-131](services/mem0/ingest.py#L112-L131) で `is_dup=True` のとき:

```python
if is_dup and not MEM0_DEDUP_DRY_RUN:
    skip_dup += 1
    if existing_id:
        await asyncio.to_thread(_reinforce_existing, mem, existing_id)
        reinforced += 1
```

新規 `services/mem0/reinforce.py`:

```python
def reinforce_existing(mem, memory_id: str, *, max_history: int = 10) -> bool:
    """Increment mention_count + bump importance on an existing fact."""
    try:
        existing = mem.get(memory_id)
        meta = dict(existing.get("metadata") or {})
        now = int(time.time())
        meta["mention_count"] = int(meta.get("mention_count", 1)) + 1
        history = list(meta.get("reinforced_at") or [])
        history.append(now)
        meta["reinforced_at"] = history[-max_history:]
        meta["last_reinforced_at"] = now
        imp = int(meta.get("importance", 3))
        meta["importance"] = min(imp + 1, 5)
        mem.update(memory_id=memory_id, metadata=meta)
        return True
    except Exception as exc:
        log.warning("reinforce failed for %s: %s", memory_id, exc)
        return False
```

### search rerank での boost

[services/mem0/search.py:_rerank_memories](services/mem0/search.py#L126) で LLM 呼出前に score 加算:

```python
import math
ALPHA_MENTION = 0.10   # 重み: log(1+0.10*N) の感度
ALPHA_RECENCY = 0.05   # 最終 reinforced からの経過の感度

for m in memories:
    meta = m.get("metadata") or {}
    mention = int(meta.get("mention_count", 1))
    last_r = int(meta.get("last_reinforced_at") or 0)
    # frequency boost: log で diminishing returns
    boost_freq = math.log1p(ALPHA_MENTION * mention)
    # recency boost: 最後の reinforced が 30日以内なら +
    days_since = (time.time() - last_r) / 86400 if last_r else 365
    boost_recency = ALPHA_RECENCY * math.exp(-days_since / 30)
    m["score"] = (m.get("score") or 0) + boost_freq + boost_recency

# 既存 LLM rerank に渡す前に score 順でソートしておくと candidate 上位 5 件が変わる
memories.sort(key=lambda x: x.get("score", 0), reverse=True)
```

LLM rerank は既存のまま。boost は score 加算で済む。

### Phase 2 (既存 fact への遡及): mass reinforcement

cleanup-plan の Phase 2 を改訂:

旧: 「同一事実の重複を検出して keep 1 件 + delete 残り」
新: 「同一事実クラスターを検出して keep 1 件 + 削除する代わりに **クラスター内の `mention_count` を合計して keep に集約**」

```python
# scripts/mem0_reinforce_existing.py
clusters = build_dup_clusters(facts, threshold=0.80)  # Sonnet judge 込み
for cluster in clusters:
    keep = max(cluster, key=lambda f: (len(f.memory), f.importance))
    total_mentions = sum(int(f.metadata.get("mention_count", 1)) for f in cluster)
    earliest = min(f.metadata.get("reinforced_at", [now])[0] for f in cluster)
    keep.metadata["mention_count"] = total_mentions
    keep.metadata["reinforced_at"] = sorted({
        ts for f in cluster for ts in (f.metadata.get("reinforced_at") or [])
    })[-10:]
    keep.metadata["last_reinforced_at"] = max(...)
    keep.metadata["importance"] = min(5, max(int(f.metadata.get("importance", 3)) for f in cluster) + 1)
    mem.update(keep.id, metadata=keep.metadata)
    for f in cluster:
        if f.id != keep.id:
            mem.delete(memory_id=f.id)
```

---

## 修正対象ファイル

| ファイル | 変更 | フェーズ |
|---|---|---|
| 新規 [services/mem0/reinforce.py](../../services/mem0/reinforce.py) | `reinforce_existing()` ヘルパー | 1 |
| 編集 [services/mem0/dedup.py](../../services/mem0/dedup.py) | `check_near_dup` の戻り値に `existing_id` 追加 | 1 |
| 編集 [services/mem0/ingest.py](../../services/mem0/ingest.py) | is_dup 時に `reinforce_existing()` 呼出 | 1 |
| 編集 [services/mem0/search.py](../../services/mem0/search.py) | `_rerank_memories` で freq/recency boost | 2 |
| 編集 [services/mem0/extract.py](../../services/mem0/extract.py) | extract 出力に `mention_count: 1` 初期値 (オプション、ingest 側で default 入れても可) | 1 |
| 新規 [scripts/mem0_reinforce_existing.py](../../scripts/mem0_reinforce_existing.py) | 既存 fact 群に遡及して mass reinforcement (cleanup Phase 2 の改訂) | 3 |
| 新規 [tests/test_mem0_reinforce.py](../../tests/test_mem0_reinforce.py) | reinforce_existing の単体テスト + rerank boost の検証 | 1, 2 |

---

## 実装フェーズ

### Phase 1: 新規 ingest 経路の reinforcement (約 2 日)

1. `reinforce.py` 実装 + 単体テスト
2. `dedup.py` の signature 変更
3. `ingest.py` 配線
4. ingest 1 ターン分の dry-run で `mention_count` が増えるのを実機確認
5. bot 再起動して本番運用

### Phase 2: search rerank boost (約 1 日)

1. `search.py` で score 加算ロジック追加
2. テストで boost が想定通り動くか確認 (mention_count=10 と 1 の fact で順位差が出ること)
3. 本番運用

### Phase 3: 既存 fact への遡及 mass reinforcement (約 1 日 + LLM 数十分)

1. `scripts/mem0_reinforce_existing.py` 実装
2. dry-run で「集約予定クラスター」のサンプル目視確認 (10 例程度)
3. 本番実行 (cleanup-plan Phase 2 の改訂版として)
4. DB の `mention_count` 分布をログで確認 (平均 / max / 1 件しかない fact の比率)

---

## 検証

### Phase 1
```bash
.venv/bin/python -m pytest tests/test_mem0_reinforce.py -v
# reinforce_existing で mention_count が +1 されること
# reinforced_at が最新 10 件に trim されること
# importance が 5 で頭打ちになること
```

実機: bot で「先輩は Rust が好き」を 2 ターンに渡って言う
```bash
sqlite3 .state/mem0_db/chroma.sqlite3 \
  "SELECT key, string_value, int_value FROM embedding_metadata
   WHERE key IN ('mention_count', 'last_reinforced_at')
   ORDER BY rowid DESC LIMIT 10;"
```

期待: その fact の `mention_count` が 2 になっていること。

### Phase 2
```python
# 同じ query で boost 前後を比較
candidates_before_boost = await search_memories_multi(q, uid, limit=20)
candidates_after_boost = await _rerank_memories(q, candidates_before_boost, top_k=5)
# mention_count が高い fact が上位に来ているか
```

### Phase 3
```bash
sqlite3 .state/mem0_db/chroma.sqlite3 \
  "SELECT count(*) FROM embedding_metadata WHERE key='mention_count' AND int_value > 1;"
# 期待: クラスター統合された fact が数十〜数百件 mention_count > 1
```

---

## ロールバック

- `mention_count` 等は metadata 上の追加フィールド。読まなければ既存ロジックに影響なし
- `_rerank_memories` の score 加算は ALPHA を 0 にすれば boost ゼロ → 既存挙動と等価
- `reinforce_existing` を呼ばないようにすれば dedup は従来通り drop
- 各環境フラグで kill switch を持たせる:
  - `MEM0_REINFORCE_ENABLED=true` (default true)
  - `MEM0_BOOST_ALPHA_MENTION=0.10`
  - `MEM0_BOOST_ALPHA_RECENCY=0.05`

---

## オープン課題

1. **importance の cap**: 5 で頭打ちにすると「100 回言及」と「6 回言及」が区別できない。`mention_count` を独立して持つので問題ないが、importance への昇格幅は要調整 (現案: +1/重複)
2. **reinforced_at の保持件数**: 最新 10 件で十分か。長期的な reinforcement の trend を見たいなら 50 件等に拡大
3. **boost 係数 (ALPHA)** の妥当性: A/B で「boost あり」「boost なし」を切り替えて先輩の体感比較が理想
4. **「忘却」との関係**: 古い fact の tier 降格 (weekly compact) と reinforcement の優先順位。最近 reinforced されたら tier を上げる (Live に戻す) 処理を追加する選択肢
5. **cleanup-plan との整合**: cleanup-plan の Phase 2 (mass dedup) は本計画 Phase 3 で代替する。docs を相互参照

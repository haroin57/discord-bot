# Mem0 品質改善計画: ノイズ削減・重複統合・時系列タグ

**作成**: 2026-05-14
**目的**: ingest 完走後の Mem0 観察で発覚した品質課題を構造的に解消する

---

## 1. Context — 観察された 3 つの課題

### A. 粒度バラバラ
意味の薄い fact (例: `"Restarted/Rebooted something"`) とアーキテクチャ詳細設計の fact が同じ collection に混在。ノイズが rerank の質を下げる。

### B. 重複
- admin 権限関連の話が 4-5 件
- 「dev ブランチに直接 push 禁止」が複数表現で並ぶ
- 現状の dedup (cosine 0.95 hard / 0.80 LLM judge) でも表現違いを取りこぼしている

### C. 時系列消失
- `"RAM 2.2GB 使用中"` のような一時的状態が永続事実として保存されている
- created_at は今後 [backfill 計画](2026-05-14-mem0-timestamp-backfill-plan.md) で付与可能だが、それは「いつ言ったか」のみ
- 「**いつまで有効か (expiry)**」の概念がない

### 検索品質への影響

smart search の LLM rerank で **ある程度は救われている**が:
- rerank に渡す candidate 20 件のうち重複/ノイズが半分を占めると、本当に欲しい fact が上位 5 に来ない
- bot が「先輩は admin 権限について何度も言及してる」と認識して、応答が変になる

---

## 2. 3 段階の改善方針

| Phase | 内容 | 対象 | LLM コスト |
|---|---|---|---|
| 1 | **既存 fact の値踏み + ノイズ除去** (one-shot cleanup) | 既存 1570 件 | Sonnet 1570 call ≒ $5 |
| 2 | **重複統合** (existing facts pairwise dedup) | 既存 1570 件 | Sonnet 200-500 call ≒ $1-3 |
| 3 | **抽出 prompt 改修 + value_class メタデータ導入** (将来分の予防) | extract.py | コストゼロ (運用のみ) |

各 Phase は独立して実行可能。優先度 1 > 2 > 3。

---

## 3. Phase 1: ノイズ除去 (one-shot cleanup)

### 設計

既存 1570 fact を Sonnet で **value_class** に分類:

| value_class | 説明 | 例 | 処置 |
|---|---|---|---|
| `noise` | 意味の薄い断片、文脈不明 | "Restarted/Rebooted something", "OK", "ありがとう" | **即 delete** |
| `transient` | 一時的状態、観察時点でのみ valid | "RAM 2.2GB 使用中", "今日は 15:30 から会議", "PID 1099508 で ingest 実行中" | **delete** (時系列追跡なら別 system で) |
| `preference` | 嗜好・好み (時間的に長く valid) | "先輩は Rust が好き", "ビフォアシリーズが好き" | keep |
| `decision` | 設計判断・ルール | "dev に直接 push 禁止", "git --no-verify は使わない" | keep |
| `knowledge` | 技術知識・事実 | "Mem0 v1.1 で graph 機能廃止", "claude-cli 2.1.92 で sdk-cli entrypoint" | keep |
| `episodic` | 出来事・経験 | "2026-04-15 にユニットテストでバグ発見した" | keep (時系列価値あり) |

### Sonnet 判定 prompt

```
以下の Mem0 fact を `noise | transient | preference | decision | knowledge | episodic` の
いずれかに分類してください。

判定基準:
- noise: 意味の薄い断片、文脈不明、bot との会話のリアクションのみ
- transient: 「いま」「今日」「今月」など短期間でしか valid じゃない状態
- preference / decision / knowledge / episodic: 長期保存価値あり

fact: <text>

JSON で返してください: {"class": "...", "confidence": 0.0-1.0, "reason": "..."}
```

confidence < 0.7 のときは保守的に `keep` 扱い。

### 実装

新規 `scripts/mem0_value_cleanup.py`:
1. 全 fact ロード (約 1570 件)
2. Sonnet で並列 8 並列、delay 10s で value_class 判定
3. dry-run でレポート出力 (削除予定数、サンプル top/bottom 10)
4. 本番で `noise` / `transient` を delete
5. それ以外には `value_class` メタデータを追加 (将来の検索 boost 用)

### コスト

- Sonnet input ~400 tokens + output ~50 tokens / call
- 1570 call × (400 × $3 + 50 × $15) / 1M = **$5 程度**
- subscription overage で吸収可

### 検証

```bash
# dry-run 実行
.venv/bin/python scripts/mem0_value_cleanup.py --dry-run

# レポート例:
# noise:       42 件 (例: "OK", "Restarted something")
# transient:   78 件 (例: "RAM 2.2GB", "今日 15:30 会議")
# preference: 312 件
# decision:   188 件
# knowledge:  651 件
# episodic:   299 件

# 確認後、本番:
.venv/bin/python scripts/mem0_value_cleanup.py
```

期待: 約 120 件 (7%) が削除される。残り 1450 件は value_class メタデータ付き。

---

## 4. Phase 2: 重複統合 (mass dedup)

### 設計

既存 fact ペアで「同一事実だが表現違い」を検出して統合する。これは現行の `services/mem0/dedup.py` の 2 段階 dedup を **既存データに遡及適用** する操作。

#### ステップ

1. **候補ペア生成**:
   - 全 fact embedding を ChromaDB から読み出し
   - 各 fact の最近傍 5 件を探す
   - cosine ≥ 0.80 のペアを candidate に
2. **LLM judge**:
   - `services/mem0/dedup.py:llm_judge_same_fact` を流用
   - Sonnet で「これらは同じ事実か?」を判定
3. **マージ判断**:
   - 同一なら「最も情報量の多い fact (text 長 + importance) を残す」
   - 他は delete
4. **チェーン処理**:
   - A == B, B == C なら A == B == C で 1 件残し 2 件 delete

### 実装

新規 `scripts/mem0_dedup_existing.py`:

```python
# 擬似コード
embeddings = load_all_embeddings()   # 1570 × 1536
sims = cosine_matrix(embeddings)      # 1570 × 1570 (上三角)
pairs = [(i, j) for i, j in np.argwhere(sims >= 0.80) if i < j]

# pairs を LLM judge で同一判定
clusters = {}  # union-find
for i, j in pairs:
    if await llm_judge_same_fact(facts[i].memory, facts[j].memory):
        union(clusters, i, j)

# 各 cluster で残す fact を決定
for cluster in clusters:
    keep = max(cluster, key=lambda f: (f.metadata.get("importance", 0), len(f.memory)))
    for f in cluster:
        if f != keep:
            mem.delete(memory_id=f.id)
```

### コスト

- candidate ペア数: 0.80 閾値で約 200-500 ペア推定
- 各 1 Sonnet call ≒ $0.01 × 500 = **$5 上限**

### 検証

- dry-run でクラスター候補のサンプル出力 (上位 10 クラスター)
- 削除予定の fact が「本当に重複か」を目視確認

---

## 5. Phase 3: 抽出側の予防 (将来分)

### 設計

[services/mem0/extract.py:_EXTRACT_PROMPT](../services/mem0/extract.py) を改修:

#### 改修 1: value_class を抽出時に付与

```python
# 既存出力:
# {"facts": [{"speaker": "self", "text": "...", "importance": 3, ...}]}
#
# 改修後:
# {"facts": [{
#   "speaker": "self", "text": "...", "importance": 3,
#   "value_class": "preference|decision|knowledge|episodic",  # NEW
#   ...
# }]}
#
# 抽出時に noise / transient を判定したら、その fact を返さない (gate 強化)
```

prompt に明示的に書く:
```
以下に該当する発話は fact として抽出しないこと:
- 「OK」「ありがとう」「了解」などのリアクション
- 「いま」「今日」「今月」など短期間でしか valid じゃない状態 (例: 「RAM XX 使ってる」)
- bot 操作の確認 (例: 「Restarted」「Rebooted」)
```

#### 改修 2: ingest 側で value_class を metadata に保存

[services/mem0/ingest.py:147-158](../services/mem0/ingest.py) の `_insert_one` で:

```python
fact_meta.update({
    "category":    f.get("category"),
    "subcategory": f.get("subcategory"),
    "importance":  imp,
    "tier":        tier,
    "tags":        f.get("tags") or [],
    "value_class": f.get("value_class") or "knowledge",  # NEW
})
```

#### 改修 3: search 側で value_class boost

[services/mem0/search.py:_rerank_memories](../services/mem0/search.py#L126):

```python
# rerank LLM に「value_class が transient な fact は relevance 低めにせよ」
# と prompt で指示するか、もしくは Python 側で score 加減算
```

### 検証

extract.py 改修後、bot を 1 日運用して新規 fact の value_class 分布を確認。`noise` がゼロに近いことを成功条件とする。

---

## 6. 実行順序の推奨

```
Day 1:  Phase 1 dry-run + 結果レビュー → 本番実行 (1-2 時間)
Day 1:  Phase 2 dry-run + 結果レビュー → 本番実行 (1 時間)
Day 2:  Phase 3 (extract.py 改修 + tests) → 段階デプロイ
Day 3+: 観察、必要なら value_class 分類粒度を調整
```

bot 停止は不要 (cleanup スクリプトは Mem0 SDK の delete/update を使うので Discord 接続不要)。

---

## 7. 既存改善計画との関係

| 計画 | 関連性 |
|---|---|
| [embedder migration (3-large)](2026-05-14-mem0-embedder-migration.md) | この cleanup の **後** にやる方が良い。削除されてからの方が embedding 再計算コスト削減。dedup 閾値も再評価可能 |
| [Obsidian-graph 改善](2026-05-14-mem0-obsidian-graph-improvement.md) | entity 抽出を活用して重複統合の精度を上げる。Phase 2 (dedup) を「同じ entity に紐づく fact 群でのみ統合判定」に変えると効率化 |
| [timestamp backfill](2026-05-14-mem0-timestamp-backfill-plan.md) | Phase 1 で `transient` を消すと、`backfill_timestamps` で対象になる fact が減る。順序的には cleanup → backfill が効率的 |

**推奨順序**: cleanup (本計画) → backfill → graph → embedder migration

---

## 8. リスク

| リスク | 対応 |
|---|---|
| Sonnet 判定の誤分類で大事な fact を `noise` 判定して削除 | dry-run で全削除候補を目視確認、confidence < 0.7 は保守的に keep。さらに削除前に JSON dump バックアップ |
| Phase 2 で重要な fact が「より長い fact に統合された結果、削除された」 | 「残す fact」決定ロジックで importance / 内容詳細度 / created_at を総合判断、削除前 dump |
| 大量 delete で ChromaDB のサイズが偏る | Phase 1+2 完了後に collection compaction (Chroma 公式手順、または再 build) |

---

## 9. ロールバック

各 Phase 実行前に:

```bash
cp -a /home/ubuntu/discord-bot/.state/mem0_db \
      /home/ubuntu/discord-bot/.state/mem0_db.bak-cleanup-$(date +%Y%m%d-%H%M)
```

問題発生時:

```bash
sudo systemctl stop discord-bot
rm -rf /home/ubuntu/discord-bot/.state/mem0_db
mv /home/ubuntu/discord-bot/.state/mem0_db.bak-cleanup-* /home/ubuntu/discord-bot/.state/mem0_db
sudo systemctl start discord-bot
```

---

## 10. 次のアクション

1. このドキュメントを先輩レビュー
2. Phase 1 (ノイズ削除) からスタート
3. dry-run のレポートを見て削除閾値 (confidence) と判定基準を微調整
4. 本番実行 → 検索品質を 1 日観察
5. Phase 2 (重複統合) に進む

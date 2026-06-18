# Mem0 dedup 設計問題の洗い出し

> 作成: 2026-05-16  
> 契機: Mem0グラフ (500 nodes / 537 edges) の分析で浮かんだ構造的問題

---

## TL;DR

dedup は **fact テキスト同士の重複排除** しかカバーしていない。  
グラフ分析で可視化された問題の大半は **entity 解決 (表記揺れ統合)** と **共起検出** の欠如に起因しており、現行 dedup パイプラインのスコープ外。

---

## 1. Entity 表記揺れの未統合（最重要）

### 現象
| canonical として独立しているノード | 本来同一 |
|---|---|
| `上伊那ぼたん` (35) / `ぼたん` (20) / `花川ぼたん` (1) | 同一作品 |
| `いぶき` (29) / `砺波いぶき` (14) | 同一キャラ |
| `先輩` (9) — エッジ数ゼロの完全孤立 | haroin への言及 |
| `Ableton` (6) — エッジ数ゼロ | Ableton Live |

### 根本原因
`extract.py` の `_EXTRACT_PROMPT` が entities フィールドで `aliases` を返す仕様になっているが、**ingestion 時に aliases を entity_aliases テーブルに登録しているだけで、既存 entity との名寄せ (merge) を行っていない**。

- `graph.upsert_entity()` は `canonical_name` の完全一致で既存 entity を検索する
- `ぼたん` と `上伊那ぼたん` は canonical_name が違うので別 entity として INSERT される
- alias テーブルに `ぼたん` が入っていても、次回 `canonical_name=ぼたん` で upsert すると alias とのマッチングは起きない

### 影響範囲
- グラフの見た目が分裂する（本来1クラスタのものが3つに割れる）
- `search_memories_smart` の alias expansion (`_expand_query_via_graph`) が canonical → alias の片方向しか解決しない。`ぼたん` で検索しても `上伊那ぼたん` の fact は alias expansion でヒットしない
- `mention_count` が分散し、reinforcement boost が弱まる

### 修正案
`upsert_entity` に **alias-aware lookup** を追加する:

```
1. canonical_name 完全一致 → 既存 entity に merge
2. alias テーブル完全一致 → その alias の parent entity に merge
3. 新規 entity の aliases に、既存 canonical_name と一致するものがある → merge
4. いずれも不一致 → 新規 INSERT
```

加えて、既存データの一括クリーンアップスクリプトが必要（後述 §7）。

---

## 2. 孤立ノード「先輩」問題（entity 解決の特殊ケース）

### 現象
`先輩` が mention_count=9 あるのにエッジ数ゼロ。グラフ上で完全孤立。

### 根本原因
- 「先輩」は固有名詞ではなく呼称。extract prompt が entity として抽出するが、canonical_name が `先輩` のまま
- 誰を指すかの文脈解決 (coreference resolution) が extract 時に行われていない
- `haroin` / `佐々木暖人` / `先輩` の紐づけが存在しない

### 修正案
A. **extract prompt に呼称→固有名詞の解決指示を追加**: 「先輩」→ `haroin`、「お父さん」→ `b0nchichi` 等の既知マッピングを prompt に埋め込む  
B. **graph.upsert_entity に hardcoded alias map を持たせる**: `{"先輩": "haroin", "お父さん": "b0nchichi"}` のような辞書で、upsert 時に正規化

A の方が汎用的だが、prompt が膨らむ。B は安価だがメンテが必要。

---

## 3. 共起エッジの欠落

### 現象
- `Ableton` がエッジ数ゼロ（mention_count=6 あるのに他 entity と繋がらない）
- 技術クラスタと音楽クラスタが `ariiol + Rust` の1件でしか橋渡しされていない

### 根本原因
現在のエッジは **fact → entity** の二部グラフ。**entity ↔ entity の共起エッジ** が存在しない。

同一 fact に複数 entity が紐づいていれば、fact を経由して暗黙的に共起関係は存在するが:
- Ableton の場合、fact が「Ableton で作ってる」のように単独 entity で記録されており、他の entity と同じ fact に載っていない
- つまり **extract が1 fact に含める entity 数が少なすぎる**、あるいは **Ableton を含む fact がそもそも少ない**

### 修正案
A. **entity co-occurrence テーブルを新設**: 同一 fact に紐づく entity ペアの共起回数を集計するビューまたはマテリアライズドテーブル  
B. **extract prompt で「1 fact に複数 entity を含める」指示を強化**: 現状 `0〜3個` だが、ツール名 + アーティスト名のように自然に共起するものは両方入れるよう例示を追加

---

## 4. fact テキスト dedup の残存問題

### 4a. category フィルタの不完全性

`dedup.py:check_near_dup` は既存 fact の category と候補の category が異なれば skip する。

```python
if candidate_category and existing_category and candidate_category != existing_category:
    continue
```

問題: **category が None / 空文字のレガシー fact に対してフィルタが効かない**。gate_version=v1 時代の fact には category metadata がないため、常に `existing_category=None` で `None != "hobby"` が True になり skip される。つまりレガシー fact との dedup が全く効いていない。

### 4b. intra-batch dedup がテキスト完全一致のみ

```python
if t in seen_texts:
    skip_dup += 1
```

同一バッチ内で「Rustが好き」と「Rustを好んでいる」が両方抽出された場合、テキストが異なるため両方 INSERT される。cosine dedup は既存ストアとの比較なので、バッチ内の2件目が INSERT される前に1件目がまだストアにないため検出できない。

### 4c. reinforcement の metadata 競合

`reinforce.py` は `mem.update(memory_id, data=existing_text, metadata=partial)` を呼ぶが、Mem0 SDK の update は **data を渡すと re-embedding が走る**。

- 同一ターンで3件の duplicate が同じ existing fact を reinforce しようとすると、3回 re-embed + 3回 metadata write が並列で走る
- `mention_count` の read-modify-write が並列実行で lost update する可能性がある（現在は single process なので低リスクだが、将来的な問題）

---

## 5. extract prompt の構造的弱点

### 5a. entity canonical 表記のブレ

extract prompt は「canonical 表記で name を入れろ」と指示しているが、**何が canonical かの定義が Haiku の判断に委ねられている**。

- ある turn では `上伊那ぼたん`、別の turn では `ぼたん` が canonical として返される
- 作品名: `CLANNAD` vs `クラナド` vs `クラナド アフター`
- 人名: `bermei.inazawa` vs `bermei inazawa` vs `Bermei Inazawa`

extract prompt に **canonical 辞書（既知 entity の正式名）** を注入すれば、Haiku が既存の canonical に合わせて返す確率が上がる。

### 5b. 1 fact あたりの entity 上限が 0〜3

音楽制作の文脈で「Ableton で Namitape みたいな UK Garage を作った」という fact があった場合、entity は `Ableton`, `Namitape`, `UK Garage` の3つで枠を使い切る。4つ目以降の関連概念は落ちる。

---

## 6. search パイプラインへの波及

### 6a. alias expansion の片方向性

`search.py:_expand_query_via_graph` は `graph.find_entity(query, fuzzy=False)` で検索する。

- `上伊那ぼたん` → entity hit → aliases `["ぼたん"]` を展開 ✓
- `ぼたん` → canonical_name 不一致 → alias テーブル検索 → entity hit → 展開 ✓

ここまでは正しいが、**§1 の問題で `ぼたん` が独立 entity として存在する場合、alias 検索より先に canonical 完全一致でヒットしてしまい、`上伊那ぼたん` の fact が展開されない**。

### 6b. graph_expand の seed 数制限

`_graph_expand_candidates` は `top_seed=3` で上位3件の fact からしか1-hop 展開しない。seed に選ばれなかった fact の entity 経由のパスは探索されない。

---

## 7. 既存データのクリーンアップ

コード修正だけでは既存の860件 + 500 entity の状態は改善しない。以下のバッチ処理が必要:

### 7a. entity merge スクリプト
- 手動または LLM 支援で同一 entity のグループを特定
- 代表 canonical_name を決定し、非代表を alias に降格
- fact_entity_edges を代表 entity_id に付け替え
- 非代表 entity を DELETE

### 7b. orphan entity 掃除
- fact_entity_edges に1件もエッジがない entity を削除
- alias だけ登録されて本体が消えた entity_aliases を削除

### 7c. レガシー fact の category backfill
- gate_version=v1 の fact に `_classify_for_metadata` を走らせて category を付与
- §4a の dedup category フィルタが効くようになる

---

## 8. 優先度マトリクス

| # | 問題 | 影響度 | 実装コスト | 優先度 |
|---|------|--------|-----------|--------|
| 1 | entity 表記揺れ統合 | **最大** — グラフ分裂の主因 | 中 (upsert_entity 改修 + merge スクリプト) | **P0** |
| 2 | 「先輩」等の呼称解決 | 大 — 孤立ノード | 小 (alias map or prompt 追加) | **P0** |
| 5a | canonical 辞書注入 | 大 — §1 の再発防止 | 小 (extract prompt 修正) | **P1** |
| 7a | entity merge バッチ | 大 — 既存データ修復 | 中 (スクリプト + 手動検証) | **P1** |
| 3 | 共起エッジ | 中 — グラフ接続性 | 中 (テーブル追加 + 集計) | **P2** |
| 4a | category フィルタ修正 | 中 — レガシー fact 対応 | 小 (条件分岐修正) | **P2** |
| 7c | レガシー category backfill | 中 — §4a の前提 | 小 (バッチスクリプト) | **P2** |
| 4b | intra-batch semantic dedup | 低 — 発生頻度低い | 中 (cosine 計算追加) | **P3** |
| 5b | entity 上限緩和 | 低 — 3で大抵足りる | 小 (prompt 修正) | **P3** |
| 4c | reinforce 競合 | 低 — single process | 中 (ロック追加) | **P3** |
| 6a | alias expansion 方向性 | 中 — §1 修正で解消 | — | §1 に依存 |
| 6b | graph_expand seed 数 | 低 | 小 | **P3** |

---

## 9. 既存改善計画との差分

`docs/mem0-dedup-improvement-plan.md` (Phase 1-3) は **fact テキストの cosine dedup** に焦点を当てており、Phase 1A (2段階 dedup) と Phase 1B (category filter) は既に実装済み。

本ドキュメントで新たに特定した問題の大半は **entity 層** の設計不足であり、既存計画のスコープ外:

| 既存計画 | 本ドキュメント |
|---------|--------------|
| fact テキスト dedup (cosine + LLM) | entity 表記揺れ統合 |
| importance 閾値 | canonical 辞書注入 |
| extract prompt の嗜好表明対応 | 共起エッジ / 呼称解決 |
| dry-run モード | 既存データ merge バッチ |

→ **dedup パイプライン自体はおおむね機能している。次のボトルネックは entity resolution 層。**

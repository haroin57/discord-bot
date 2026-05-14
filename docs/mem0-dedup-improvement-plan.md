# Mem0 重複判定アーキテクチャ改善計画

## 現状アーキテクチャの問題分析

### 重複判定（偽陽性 = 保存すべきものを落としてる）

1. **cosine similarity 0.85 が厳しすぎる**
   - 「Namitapeの映像がすごい」と「Namitapeのmy terminalをリピートしてる」は意味的に近いけど別の事実
   - embedding空間で0.85超えて弾かれる可能性が高い
   - **同一アーティスト名が含まれるだけで近傍に引っかかる**

2. **検索ベースの dedup が limit=3 しか見てない**
   - 近傍3件のうち1件でも0.85超えたら即drop
   - 既存記憶が多い領域ほど弾かれやすく、**深掘りする分野ほど記録が薄くなる逆転現象**が発生

3. **意味的に異なるが表層が似てる fact を区別する仕組みがない**
   - テキスト完全一致の intra-batch dedup はOK
   - 意味レベルの区別ができていない

### 抽出（偽陰性 = 拾うべきものを拾ってない）

4. **importance < 3 を全カット**
   - 「最近 Namitape の my terminal をずっと聴いてる」は importance 2-3 の境界
   - Haikuの判定次第で落ちる

5. **「先輩に関する事実」縛りが厳しい**
   - 「この曲いい」は事実というより嗜好表明
   - extractorが fact と判定しない場合がある

---

## 改善計画

### Phase 1: Dedup 精度改善（メイン）

#### A. 2段階 dedup に変更

```
現状: cosine >= 0.85 → drop

改善:
  1. cosine >= 0.95 → 確定 drop（ほぼ同一文）
  2. 0.80 <= cosine < 0.95 → Haiku に「同一事実か？」を判定させる
  3. cosine < 0.80 → 確定 keep
```

- グレーゾーン（0.80-0.95）だけLLM判定を挟むので、コスト増は最小限
- **対象ファイル**: `ingest.py` + 新規 `dedup.py`

#### B. dedup の検索 limit を 3→5 に拡大 + category フィルタ

- 同カテゴリ内でのみ重複判定する
- 「Namitape（hobby）」と「Namitape をプロジェクトで使った（project）」が別カテゴリなら dedup 対象にならない
- **対象ファイル**: `ingest.py` の `search_memories` 呼び出し修正

### Phase 2: 抽出感度の調整

#### C. importance 閾値を 3→2 に下げる（ただし tier=2 で保存）

- importance 2 は保存するが tier=Warm として cold 化が早い
- 週次 compact で自然に消えるので容量問題にならない
- **対象ファイル**: `config.py` + `extract.py`

#### D. extract prompt に「嗜好表明」カテゴリを明示追加

- 「〜が好き」「〜をずっと聴いてる」「〜が気になる」を fact として拾うよう prompt を修正
- 現状の prompt は「事実」に寄りすぎてて preference を落としがち
- **対象ファイル**: `extract.py` の `_EXTRACT_PROMPT`

### Phase 3: 検証パイプライン

#### E. dry-run モード追加

- `MEM0_DEDUP_DRY_RUN=true` で、実際に drop せず「drop されるはずだった fact」をログ出力
- 1週間回して偽陽性率を計測してから本番適用
- **対象ファイル**: `ingest.py` にフラグ追加

---

## 実装順序

| 順序 | タスク | 対象ファイル | 優先度 |
|------|--------|-------------|--------|
| 1 | Phase 1A: 2段階 dedup | `ingest.py` + 新規 `dedup.py` | 最高 |
| 2 | Phase 1B: category filter | `ingest.py` の search_memories | 高 |
| 3 | Phase 2C: importance 2 許可 | `config.py` + `extract.py` | 中 |
| 4 | Phase 2D: prompt 修正 | `extract.py` の `_EXTRACT_PROMPT` | 中 |
| 5 | Phase 3E: dry-run モード | `ingest.py` にフラグ追加 | 低 |

## 備考

- Phase 1A が最も効果が大きい。cosine 0.85 の一発判定をLLM判定に変えることで、「同一エンティティだが異なる事実」を正しく区別できるようになる
- Phase 3E の dry-run を先に入れて計測してから Phase 1-2 を本番適用する運用も可

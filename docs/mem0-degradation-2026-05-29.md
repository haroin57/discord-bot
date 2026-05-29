# Mem0 記憶劣化バグ調査 (2026-05-29)

## TL;DR

`<retrieved_memories>` / `<self_facts>` の一部レコードが「分かち書き＋助詞落ちのキーワード列」に潰れて表示される問題の根本原因を**コードレベルで確定**した。

- **原因**: `services/mem0/bm25_index.py:search_bm25()` が返す結果の `memory` フィールドに、**FTS5 インデックス用にトークン化された文字列**（`morpho.tokenize_for_index` の出力）をそのまま入れている (`bm25_index.py:271`)。
- **発火条件**: ある fact が **BM25 単独でヒットし、vector 検索ではヒットしなかった**とき。RRF 融合時に BM25 側の dict が採用され、キーワード列が `memory` として下流に流れる。
- **保存は無傷**: 書き込みパス (`ingest.add_memories` → `_insert_one`) は完全文 `f["text"]` を ChromaDB に格納している。劣化は**取得時のみ**で、Mem0 本体のデータは壊れていない。
- **影響**: 表示・LLM 投入時のみ。再インデックス不要、`search_bm25` の数行修正で直る。データ復旧は不要。

---

## 1. 症状 (観測事実)

プロンプトに注入される記憶ブロックの一部が、文として壊れている。

**潰れている例 (`<retrieved_memories>`, 先輩 user_id):**
```
- home ubuntu manga フォルダ マンガ まとめ 管理 共有
- Issue CLAUDE md ルール 無視
- 基本 情報 シート 佐々木 所有 情報 含む 散弾 ライフル 特定 ライフル 空気
- CLAUDE md 指示 無視 push 事故 事例 ある
- kanae web 設計 計画 詳細 md まとめる 希望
```

**無傷な例 (`<self_facts>`, かなえ user_id, 同一ブロック内):**
```
- Mem0 zero-store問題のデバッグMDを `.../mem0-zero-store-debug-2026-05-24.md` に作成し、
  仮説D（user_id未指定によりかなえ側namespaceへの誤振り分け）が最も有力と判断した。
```

潰れた形は **`分かち書き + 助詞落ち + 1文字トークン除去`** = `services/mem0/morpho.py:tokenize_for_index()` の出力形式と完全一致する。

### 劣化の選択性 (これが切り分けの鍵)

| 対象 | 状態 |
|---|---|
| お父さん (`1037285234200297522`) のレコード | **全部無傷** (完全文) |
| 先輩 (`573489206240870410`) の一部レコード | **潰れている** |
| かなえ自身 (`1489178684391817336`) self_facts | **混在** (大半無傷、一部潰れ) |

→ 劣化はグローバルではなく、**特定レコードだけ**。しかも潰れているのは「manga フォルダ管理」「CLAUDE.md ルール」「散弾/ライフル情報」「web 設計計画」のような**固有名詞・専門用語を含むニッチな話題**に偏っている。

---

## 2. 切り分け過程

### 当初仮説 (棄却)

> 「Mem0 への書き込み前に要約処理が入っていて、そこが文を壊してキーワード列にしている。self_facts と retrieved_memories が同じ壊れ方をしているのは共通の要約処理を通っている証拠」

→ **棄却**。書き込みパスを読んで反証した:

- `ingest.add_memories` → `_insert_one(f)` → `mem.add([{"role":"user","content": f["text"]}], infer=False)` (`ingest.py`)
- 格納されるのは `f["text"]` = `extract_full_signals` / `extract_kanae_signals` が返す**完全な日本語 fact 文** (120字以内、助詞付き)
- morpho が呼ばれるのは `bm25_index.index_fact()` の中だけで、これは**別の FTS5 テーブルの専用カラム**に入る。Mem0 本体（ChromaDB）の本文は一切触らない。

つまり書き込みは正しい。episode 層 (`services/kanae_self/episodes.py`) が無傷なのも、Mem0 正規化パスを通らず独立保持しているため。

### 確定原因

劣化は**検索パス**にあった。詳細は次節。

---

## 3. 根本原因 (確定)

### バグの連鎖

**(1) インデックス時**: `bm25_index.py:index_fact()`
```python
tokenized = morpho.tokenize_for_index(text)   # "homeフォルダに..." → "home ubuntu manga フォルダ ..."
c.execute(
    "INSERT INTO facts_fts(fact_id, user_id, text) VALUES (?, ?, ?)",
    (fact_id, user_id, tokenized),            # ← tokenized をtextカラムに格納 (FTS5検索用、これ自体は正しい)
)
```
FTS5 の `text` カラムには、検索精度のためにトークン化済みキーワード列が入る。**これは設計通りで正しい** — unicode61 トークナイザが日本語を語単位で引けるようにするため。

**(2) 検索時**: `bm25_index.py:search_bm25()` ← **ここがバグ**
```python
rows = c.execute(
    "SELECT fact_id, text, bm25(facts_fts) AS score FROM facts_fts WHERE ...",
).fetchall()
...
out.append({
    "id": r["fact_id"],
    "memory": r["text"],   # ← BUG: トークン化済みキーワード列を memory として返している
    "rank": i + 1,
    "bm25_score": float(r["score"]),
})
```
`memory` フィールドに、検索用にトークン化した `text` カラムをそのまま詰めている。本来の fact 本文ではない。

**(3) 融合時**: `bm25_index.py:rrf_merge()`
```python
def _bump(item, rrf_rank):
    fid = item.get("id") or ...
    existing = fused.get(fid)
    if existing is None:
        merged = dict(item)        # BM25単独ヒットだと、memory=キーワード列のまま採用される
        ...
        fused[fid] = merged
    else:
        existing["rrf_score"] += score_inc   # 既出idは rrf_score と score だけ更新、memory は上書きしない
```
- vector_hits を先に `_bump` するので、**vector でも当たった fact** は最初に `memory`=完全文（Mem0 由来）で登録される → BM25 が後から同じ id を見ても memory は上書きされない → **無傷**。
- **BM25 でしか当たらなかった fact** は、vector_hits に存在しない → BM25 item (`memory`=キーワード列) がそのまま `fused` の基底 dict になる → **潰れる**。

**(4) 表示時**: `format.py:format_memories_for_prompt()`
```python
text = m.get("memory", m.get("text", ""))   # キーワード列がそのまま採用される
lines.append(f"- {_scrub_fact_text(text)}")
```

### なぜ selective なのか / なぜ topic 相関するのか

ハイブリッド検索 (`search_memories_hybrid`) は **vector (cosine) と BM25 を並列実行して RRF 融合**する。BM25 はそもそも「**固有名詞・専門用語で cosine が取りこぼす fact を救済する**」目的で Phase A-1 で追加されたもの (`bm25_index.py` docstring 参照)。

つまり:
- **ありふれた話題の fact** → cosine で普通にヒット → vector 経由で memory=完全文 → 無傷
- **ニッチな固有名詞を含む fact** (manga 管理 / 銃器情報 / web 設計計画 等) → cosine が他クラスタに引っ張られて取りこぼす → **BM25 単独ヒット** → memory=キーワード列 → 潰れる

BM25 が「本来の救済対象」をちゃんと拾っているのに、拾った結果の**表示テキストだけが壊れている**という構図。お父さん側が全部無傷なのは、その会話群が cosine でヒットする話題だったから（=BM25 単独ヒットが発生しなかった）と説明できる。

### 無傷が確認できる他経路 (反証として)

- **graph 1-hop 展開** (`search.py:_graph_expand_candidates`): `mem.get(fid)` で ChromaDB から本文取得 → `memory`=完全文 → 無傷
- **episode bundle 展開** (`search.py:get_episode_facts`): `mem.get_all` → 本文取得 → 無傷

→ ChromaDB を直接引く経路は全て無傷。**FTS5 の text カラムを memory に流用している `search_bm25` だけが壊れている**ことが、これらの対照からも裏付けられる。

---

## 4. 影響範囲

| 項目 | 影響 |
|---|---|
| Mem0 本体データ (ChromaDB) | **影響なし**。完全文で正しく格納済み |
| BM25 FTS5 インデックス | **影響なし**。トークン化は設計通り |
| 取得 → プロンプト投入 | **影響あり**。BM25 単独ヒット fact が読めない形で LLM に渡る |
| 検索の recall (件数) | **影響なし**。fact 自体は正しくヒットしている。id も正しい |
| dedup / reinforce | 軽微。`check_near_dup` は candidate text と既存 memory を比較するが、既存側がキーワード列だと cosine distance は別途 ChromaDB が計算するため影響は限定的 |

要するに「**正しく検索できているのに、表示テキストだけ壊れている**」。LLM がその fact の内容を正確に読めないので、記憶の実効性が落ちる。データ毀損ではないので**修正は前向きのみ、復旧不要**。

---

## 5. 修正案 (優先度順)

### 案A: FTS5 に原文カラムを追加 (推奨・本筋)

`facts_fts` に `orig_text UNINDEXED` カラムを足し、原文とトークン化текストの両方を保存。`search_bm25` は `orig_text` を `memory` として返す。

```sql
CREATE VIRTUAL TABLE facts_fts USING fts5(
    fact_id UNINDEXED,
    user_id UNINDEXED,
    orig_text UNINDEXED,   -- 原文 (memory として返す用)
    text,                  -- トークン化済み (検索用)
    tokenize = 'unicode61 remove_diacritics 1'
);
```

- `index_fact(fact_id, text, user_id)`: `orig_text=text`, `text=tokenized` を両方 INSERT
- `search_bm25`: `SELECT ... orig_text ...` して `"memory": r["orig_text"]` で返す
- `reindex_all`: 同様に両方格納
- **FTS5 はスキーマ変更時にファイル wipe が必要** (`reindex_all` が既に `-wal`/`-shm` ごと unlink する実装になっている)。`scripts/build_mem0_bm25_index.py` で再構築する

**メリット**: 追加の ChromaDB lookup ゼロ。検索レイテンシ不変。
**デメリット**: スキーマ migration + 全 fact 再インデックスが必要 (~1500 件、数秒)。

### 案B: rrf_merge で BM25 単独ヒットの本文を解決

融合後の最終 `limit` 件だけ、`memory` がキーワード列のものを `mem.get(fid)` で本文に差し替える。

**メリット**: スキーマ変更・再インデックス不要。
**デメリット**: BM25 単独ヒット件数ぶん ChromaDB lookup が増える (最大 `limit` 回)。レイテンシ微増。

### 案C: search_bm25 内で mem.get して memory を埋める

`search_bm25` が返す前に各 fact の本文を ChromaDB から引く。

**デメリット**: BM25 は `MEM0_BM25_LIMIT=20` 件返すので lookup が最大 20 回/クエリ。案B より無駄が多い (融合で捨てられる分も引く)。非推奨。

### 推奨

**案A**。検索ホットパスに lookup を足さないのが筋。FTS5 のストレージ増は `orig_text` ぶんだけで、~1500 件規模では無視できる。`reindex_all` が既に wipe-and-rebuild 対応済みなので migration コストも低い。

---

## 6. 検証方法

修正後の確認手順:

1. 単体: `tests/test_mem0_bm25.py` (存在すれば) に「`search_bm25` の `memory` が原文と一致する」アサートを追加
2. 結合: BM25 単独ヒットを意図的に作る (cosine で外れる固有名詞 fact を仕込む) → `search_memories_hybrid` の結果の `memory` が完全文か確認
3. 実地: 再インデックス後、本バグで潰れていた「manga フォルダ管理」等のクエリを投げて `<retrieved_memories>` が完全文で出るか目視

再インデックス:
```bash
python3 scripts/build_mem0_bm25_index.py   # 全 fact を ChromaDB から dump → FTS5 再構築
```

---

## 7. 関連ファイル

| ファイル | 役割 | バグとの関係 |
|---|---|---|
| `services/mem0/bm25_index.py` | FTS5 インデックス + BM25 検索 | **`search_bm25():271` がバグ本体**。`index_fact` / `reindex_all` / `_SCHEMA` も案A で修正対象 |
| `services/mem0/morpho.py` | Janome 形態素解析 | `tokenize_for_index` がキーワード列を生成 (正しい挙動) |
| `services/mem0/search.py` | hybrid 検索 / smart 検索 | `search_memories_hybrid` → `rrf_merge` 経由でキーワード列が伝播 |
| `services/mem0/format.py` | プロンプト用整形 | `format_memories_for_prompt` が潰れたテキストを最終描画 |
| `services/mem0/ingest.py` | 書き込み | **無実**。完全文を格納している |
| `services/kanae_self/episodes.py` | episode 層 | **無実**。独立保持で無傷 |
| `scripts/build_mem0_bm25_index.py` | 再インデックス | 案A 適用後に実行 |

---

## 補足: 当初の self-referential 仮説について

調査初期は「dedup 実装時にキーワード抽出した正規化テキストを本文として保存する自己参照バグ」を疑ったが、これは誤り。dedup (`check_near_dup`) は candidate の生テキストで比較しており、正規化テキストを書き戻す経路は存在しない。劣化の発生時期が「dedup 実装期」と相関して見えたのは偶然で、実際は **BM25 ハイブリッド検索 (Phase A-1) 導入以降に蓄積した、cosine が取りこぼすニッチ fact** が BM25 単独ヒット経由で表面化していたため。原因の所在は dedup ではなく BM25 検索の戻り値整形にある。
</content>
</invoke>

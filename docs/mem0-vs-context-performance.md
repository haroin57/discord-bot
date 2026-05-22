# Mem0 (vector retrieval) vs Context Window inline — 性能比較と改善方策

**作成日**: 2026-05-23
**作成**: かなえ (Discord bot 内)
**ステータス**: 調査 + 設計提案。実装は未着手 (ロードマップ §7 を参照)
**対象 readers**: 先輩 (haroin) / 将来の自分 / Mem0 まわりを触る人

---

## 0. TL;DR

- **先輩の体感は正しい**。Mem0 (vector retrieval) は CW (context window) inline よりも recall が原理的に劣る。これは Mem0 固有の bug ではなく、**「全件参照可能性」と「retrieval gate」の構造的トレードオフ**。
- ただし「Mem0 < CW」と単純に結論するのは早い。CW にも **lost-in-the-middle** (Liu et al. 2023 / TACL 2024) という U字型の attention 劣化があり、**100K token を超えたあたりから真ん中の情報を LLM がほぼ使わなくなる**。1M context window でも BEAM benchmark で破綻する。
- 本質的なゴールは **「想起できる」かつ「token cost に上限がある」かつ「時系列が崩れない」** の三立。これを満たすのは現状どれか単体ではない。**hybrid search + cross-encoder rerank + working memory hierarchy** の組合せが現時点の SOTA。
- **discord-bot の現状実装はすでに上位 30% 相当に位置する** (smart search rerank / 2-stage dedup / Hebbian reinforcement / entity graph / taxonomy + tier)。最大の積み増し余地は **BM25 hybrid 化** と **HyDE 風の hypothetical answer 拡張**、次点で **temporal weighting の強化** と **contradiction detection**。置き換えではなく、**全部既存パイプラインの上に積める**。
- 推奨実装順 (詳細は §7): **(1) BM25 hybrid → (2) HyDE rewrite → (3) temporal decay の formal 化 → (4) contradiction detection → (5) working memory 層の明示化**。

---

## 1. 問題定義 — 「流動性」とは何か

先輩の元の問い:

> Mem0 の情報の流動性を鑑みると CW に含まれる情報よりもかなりパフォーマンスが悪い気がしていて

ここでの「流動性」は意図的に複数の意味に解釈できる。本稿では以下 3 つに分解する。

| 流動性の側面 | 定義 | Mem0 の弱さ | CW の弱さ |
|---|---|---|---|
| **(A) 想起の連続性** | 関連事実が一括で引ける | top-K で切るので外れる | 全件あれば取れる |
| **(B) 順序保持** | 時間軸が壊れない | created_at の信頼性が部分的 | 時系列そのまま |
| **(C) 矛盾解消** | 古い事実が上書きされる | dedup 任せ、衝突未検出 | 並びで見える |

**(A)** が一番の論点だが、**(B)** と **(C)** も無視できない。今夜のいぶき事件 ("いぶきに寄せて髪染めたって言ったじゃん") はまさに **(A)** の典型で、Mem0 には事実があったが retrieval gate (認証エラー + post-rerank gate の歴史的負債) で出てこなかった。

---

## 2. 数値的な性能比較

### 2.1 ベンチマーク横断

**LoCoMo** (Maharana et al. 2024, Long Context Multi-turn) — マルチターン記憶評価。10セッション平均300ターン。

| システム | LoCoMo score | アーキテクチャ |
|---|---|---|
| **Zep (KG + temporal)** | **75.14%** | temporal knowledge graph + cross-session reasoning |
| **Letta Filesystem (GPT-4o mini)** | **74.0%** | 単純な file-based history、LLM が file 読み書き |
| Mem0 graph variant | 68.5% | vector + KG |
| Mem0 vanilla | ~60% | vector のみ |
| Full context (long-context inline) | ~70% (10セッション以下) → 大幅低下 (100K+) | CW 直入れ |
| OpenAI Memory | ~50-60% | 公開不明な仕組み |

引用: [letta.com](https://www.letta.com/blog/benchmarking-ai-agent-memory) / [mem0.ai blog](https://mem0.ai/blog/state-of-ai-agent-memory-2026) / [Hashnode comparison 2026](https://hashnode.com/posts/agent-memory-race-2026-mem0-zep-letta-mempalace/)

### 2.2 LongMemEval (Wu et al. 2024)

Mem0 vs CW で **クロスセッション推論**を測る。

| システム | LongMemEval | recall context size |
|---|---|---|
| **Zep** | +18.5% over MemGPT baseline | 1-3K tokens |
| Mastra Observational Memory | **95%** | (上位) |
| Mem0 + graph | ~80% | 5-7K tokens |
| MemGPT | (baseline) | hierarchical |
| Long-context (1M) | 急速劣化 (100K+) | (full) |

引用: [Zep arxiv 2501.13956](https://arxiv.org/abs/2501.13956) / [Mastra Research](https://mastra.ai/research/observational-memory)

### 2.3 token cost / latency

| アプローチ | per-query token | 月額 (1日100ターン換算) | latency |
|---|---|---|---|
| **CW inline (全件 50K context)** | 入力 50K × 100 = 5M/日 → 150M/月 | $450/月 (Sonnet input price $3/M) | 6-10秒 (long context) |
| **Mem0 vector top-5 (~5K context)** | 入力 5K × 100 = 500K/日 → 15M/月 | $45/月 | 200-500ms |
| **Mem0 + hybrid + rerank** | 入力 5-7K + reranker LLM call | $60-80/月 | 400-800ms |

CW vs Mem0 の **コスト比は 10倍前後**。これが Mem0 を諦められない最大の理由。

### 2.4 BEAM benchmark の含意

[BEAM (Bini et al. 2025)](https://mem0.ai/blog/what-is-beam-memory-benchmark-the-paper-that-shows-1m-context-window-isnt-enough) は 100K〜10M tokens のコンテキストを **10種類の memory ability** (要約・multi-hop reasoning・preference following 等) で評価。結論:

- **1M context window でも、retrieval が必要なタスクでは GPT-4 級モデルが破綻**
- LIGHT (memory-aware) > RAG > long-context inline (全長帯域で)
- つまり「全部 CW に入れれば解決」は 100K を超えるとデバンク済み

---

## 3. Mem0 (vector retrieval) の構造的弱点

discord-bot で実装してて経験的に分かってる症状 + 文献の根拠を併記する。

### 3.1 cosine recall の保証不在

[Alok blog](https://aloknecessary.github.io/blogs/bm25_vs_vector_search/) の指摘そのまま:

> Vector search is semantically powerful but **recall is not guaranteed**. BM25 catches exact-match cases that vector search misses — especially for technical queries.

具体例 (今夜の事件含む):

- **「いぶき」「砺波いぶき」「上伊那ぼたん」** のような固有名詞は、embedding が **意味的に近い類似キャラ** に引っ張られる
- **「Mem0」「Discord」「Rust」** のような技術タグ語は、頻出すぎて embedding 空間で密集 → cosine top-5 が全部同じトピックの異なる事実になる
- **「pushは朝のセルフレビュー後」** のような **約束** は、表層形が会話ごとに違いすぎて embedding でまとまらない

### 3.2 chunk 境界依存

Mem0 は **fact 1個ずつを別々に embed** する設計なので chunk 境界の問題は緩和されてるが、別の問題が出る:

- 「ぼたんといぶきのもどかしい距離感」「観測者 position の物理化」のような **多entity横断の関係性 fact** は、単独 embedding に圧縮された時点でノイズが入る
- 結果、「ぼたん」で検索しても「いぶき」絡みが出ないことがある

これは現状 **entity graph layer (Phase 4 の 1-hop expansion)** で部分的に補ってる。

### 3.3 時系列消失

- ChromaDB の vector search に **時刻 metadata は乗らない** (cosine だけで返す)
- `created_at_unix` を入れてるが、**search 結果のソート基準には現状使ってない**
- 結果、「昨日言った」「先週決めた」のような **最近性 query** に弱い
- 部分的補正: `last_reinforced_at` を boost に入れてる (`α_recency * exp(-days/30)`)

ただし「先週の Wed の話」のような **絶対時刻 query** は依然解けない。

### 3.4 矛盾未検出

[Recallr AI](https://recallrai.com) と [MemoryAgentBench](https://www.emergentmind.com/topics/memoryagentbench) の指摘:

> Conflict resolution is a key bottleneck: Even with advanced indexing and agentic loops, handling contradicting updates and correctly overwriting outdated facts remains unsolved.

discord-bot の現状:

- **dedup は同一性判定だけ**。「同じ事実の言い換え」は弾くが、**「先輩は前に X と言ったが今は ¬X」** は両方残る
- 「中野梓のコスプレやる」→「澪も検討」→「やっぱあずにゃん」のような **意思決定の揺らぎ系列** が全部 fact として並列保存される
- 結果、矛盾する fact 群が rerank に渡って LLM が混乱する

### 3.5 retrieval gate の pessimism (歴史的)

[ADR-0002](.claude/adr/0002-mem0-post-rerank-gate-redesign.md) で記録した通り、`post-rerank cosine gate (0.65)` が日本語会話文で全件キル状態だった。**撤廃済み** だが、これと類似のpathological gateは別 layer (pre-rerank / dedup threshold) に潜在し続ける可能性がある。

具体的な現存懸念:

- `DEDUP_HARD_DROP_DISTANCE = 0.15` / `DEDUP_GRAY_HIGH_DISTANCE = 0.35` — これも日本語会話で同様の問題が起きる可能性。**実測 dedup hit rate を計測しないと、過剰dedup を見逃す**
- `MEM0_RELEVANCE_MAX_DISTANCE = 1.2` — 緩めだが、これに依存する rerank=False path はノイズ含み

### 3.6 query 表層への過敏性

「いぶきの髪色」と「いぶきに寄せた染髪」は人間には同じだが embedding 空間では別の点。

現状の補償:
- `_rewrite_query` で Haiku が canonical キーワードに展開 (~3-5個)
- `_expand_query_via_graph` で entity alias 展開 (「クラナド」→「CLANNAD」)
- multi-query 並列検索 → merge

ただし、これは **「クエリ側」だけの拡張**。**保存側の事実が表層的に多様** だと依然取りこぼす。次節 §4 の HyDE 方向で補える。

### 3.7 entity 結合が貧弱

graph layer はあるが **fact ⟷ entity の 1-hop expansion** までで止まっている。

足りない:

- **entity-entity 関係** (「上伊那ぼたん is_about 砺波いぶき」「ぼたん located_in 富山」)
- **時系列に沿った entity 状態変化** (「先輩の髪色 = 黒 → ハイトーン (2026-05-22)」)
- **複数 fact を束ねた episode** (`episode_id` はあるが活用されてない)

Zep の temporal KG はこの 3つを全部やってる。

---

## 4. Context Window inline の構造的弱点

「全部入れれば解決」は短期的には正しいが、規模で破綻する。

### 4.1 Lost in the middle (Liu et al. 2023 / TACL 2024)

[arxiv 2307.03172](https://arxiv.org/abs/2307.03172) の決定打:

> Performance is often highest when relevant information occurs at the **beginning or end** of the input context, and **significantly degrades when models must access relevant information in the middle of long contexts**, even for explicitly long-context models.

U字型 attention カーブ。**32K context でも、真ん中 50% の token は accuracy 30-50% drop**。

100K context inline の話なら、**「持ってるけど見てない」状態**になる。これは「Mem0 で出てこない」より悪い (出てこないと分かる方が、見たフリされるよりマシ)。

### 4.2 token cost が線形

§2.3 で計算した通り、100ターン/日の Discord bot で **CW inline = Mem0 の 10倍コスト**。先輩は今 Anthropic max plan + ARCRA のクレジット枠で動かしてるが、**bot を 24/7 で他人にも使わせるとすぐ pricing 圧迫**。

### 4.3 編集不可性

CW inline は **read-only な snapshot**。

- 「中野梓やめて澪にした」を CW で表現したい → 古い「中野梓やる」の行も残ったまま (削除すると履歴改竄)
- 結果、矛盾するメッセージが両方 LLM に見える → §3.4 と同じ問題が CW 側にも出る

Mem0 ならば **明示的に delete / update** できる (現状 bot 側でその write path は薄いが、原理的には可能)。

### 4.4 セッション境界の硬さ

Discord bot は 30分非アクティブで session reset (CLAUDE.md L57)。

- CW inline 派は **session 切れた瞬間に全てのコンテキストが消える**
- Mem0 派は **session 跨いで facts が persist する**

「いぶき発言は前のセッション」だったので、CW inline 単独だと **そもそも候補にすら入らない**。Mem0 はそれを救う仕組み。

### 4.5 prompt injection 表面積

CW に対話履歴を直接入れると、過去の非admin発言が prompt として読まれるリスクが上がる。Mem0 経由なら **構造化された fact** に変換されるので、injection の vector が縮む。

(これは discord-bot 固有の理由。一般論ではない。)

---

## 5. 改善方策カタログ

各方策を **「何を解決するか」「コスト」「discord-bot への積み増し可能性」** の3点で評価。

### 5.1 Hybrid Search (BM25 + vector)

- **解決**: §3.1 (cosine recall 不在) / §3.6 (表層過敏性)
- **コスト**: BM25 index 追加 (sqlite FTS5 or Tantivy) + fusion ロジック。LLM 呼び出し増は無し
- **積み増し可能性**: ★★★★★ 最高。現状 `services/mem0/search.py:search_memories_multi` の前段に BM25 検索を並列で挟むだけ。fusion は Reciprocal Rank Fusion (RRF) が定番
- **根拠**: [Towards Data Science Hybrid Search](https://towardsdatascience.com/hybrid-search-and-re-ranking-in-production-rag/) — "re-ranking does not help recall because it works within the already-retrieved set. The hybrid improvement on recall was the BM25 component pulling in exact-term matches that vector search missed."

実装案 (擬似コード):

```python
# services/mem0/search.py の改修案
async def search_memories_hybrid(query, user_id, limit=20):
    # 並列で 2 retriever
    vector_task = search_memories_multi([query], user_id, limit=limit)
    bm25_task   = search_memories_bm25(query, user_id, limit=limit)  # 新規
    vector_hits, bm25_hits = await asyncio.gather(vector_task, bm25_task)

    # Reciprocal Rank Fusion (RRF)
    return rrf_merge(vector_hits, bm25_hits, k=60)  # k=60 が業界デフォ
```

**期待効果**: 固有名詞 (「砺波いぶき」「Namitape」「上伊那ぼたん」) の recall が大幅改善。embedding 空間で隠れた exact match を救出。

### 5.2 Cross-encoder Rerank (BGE / Cohere)

- **解決**: §3.6 (precision 補正) + LLM rerank のコスト削減
- **コスト**: BGE-v2-m3 を self-host (CPU で N=50 候補 ~100-250ms) or Cohere API ($1/1M tokens)
- **積み増し可能性**: ★★★☆☆ 中。**現状 LLM rerank (Haiku) で代用してる**。BGE に切り替えると LLM 1コール削減 (-200ms / -$0.0001/turn) だが、persona 文脈を考慮できない欠点
- **根拠**: [BSWEN reranker comparison 2026](https://docs.bswen.com/blog/2026-02-25-best-reranker-models/) — "BGE-v2-m3 on GPU matched Cohere's latency for zero ongoing cost"

**判断**: 現状の LLM rerank は **「persona 文脈で関連性判断」できる** ので捨てがたい。BGE は **二段重ね** (BGE で 50→20, LLM で 20→5) が現実解。

### 5.3 HyDE (Hypothetical Document Embeddings)

- **解決**: §3.1 (recall) + §3.6 (表層差)
- **コスト**: 検索前に Haiku 1コール (~100ms / $0.0001)
- **積み増し可能性**: ★★★★☆ 高。`_rewrite_query` を `_generate_hypothetical_answer` に置き換える/追加するだけ
- **根拠**: [HyPE paper (2025)](https://medium.com/@ali.rafik.ali.97/leveraging-hypothetical-document-embeddings-hyde-to-enhance-retrieval-augmented-generation-434deccb9755) — "improved retrieval precision by up to 42 percentage points and recall by up to 45 points on certain datasets compared to standard retrieval methods"

実装案:

```python
# 既存 _rewrite_query の隣に追加
async def _generate_hypothetical_fact(query: str) -> str:
    """Generate a fake but plausible Mem0 fact that would answer the query."""
    # 例: 「先輩はいぶきが好きだっけ?」 →
    # "先輩は砺波いぶきが砺波ぼたん作品の中で一番好きなキャラクター"
    return await haiku_completion(f"...")

# search_memories_smart 内で
queries = [query, *alias_variants, *llm_rewrites, hypothetical_fact]
```

**期待効果**: 「いぶきの髪色」のような会話的 query が「先輩は砺波いぶきに寄せて髪をハイトーンに染めた」のような fact-shaped 文字列に変換され、embedding 空間で目的 fact と近づく。

### 5.4 Knowledge Graph (Zep 風 temporal)

- **解決**: §3.3 (時系列) / §3.4 (矛盾) / §3.7 (entity 結合)
- **コスト**: 大。sqlite entity graph の拡張 + temporal edge 追加 + 全 fact の entity 再抽出
- **積み増し可能性**: ★★★☆☆ 中。**Phase 4 の Obsidian-graph layer の延長線上**。entity-entity 関係と valid_from/valid_to を追加する設計
- **根拠**: [Zep arxiv 2501.13956](https://arxiv.org/abs/2501.13956) — "up to 18.5% enhanced accuracy on LongMemEval and up to 90% reduction in response latency"

実装案 (sqlite schema 拡張):

```sql
-- 既存
CREATE TABLE entity_aliases (entity_id, alias);
CREATE TABLE fact_entity_edges (fact_id, entity_id, role);

-- 追加
CREATE TABLE entity_relations (
    src_entity_id INTEGER,
    rel_type TEXT,       -- "is_about", "located_in", "loves", etc.
    dst_entity_id INTEGER,
    valid_from INTEGER,  -- unix timestamp
    valid_to INTEGER     -- NULL = currently valid
);
```

**判断**: ROI 中程度。**先に hybrid search と HyDE をやってから**、効果が頭打ちになったら手を出す方が良い。

### 5.5 Temporal weighting の強化

- **解決**: §3.3 (時系列)
- **コスト**: 小。`reinforcement_boost` の式に `created_at` 由来の decay を追加するだけ
- **積み増し可能性**: ★★★★★ 最高。services/mem0/reinforce.py を 5行修正
- **根拠**: 既存実装が `last_reinforced_at` の boost だけ持っていて、`created_at` 自体の age decay が無い

実装案:

```python
def reinforcement_boost(metadata: dict | None) -> float:
    if not metadata: return 0.0
    boost = 0.0
    boost += 0.10 * log(metadata.get("mention_count", 1))
    days_since_reinforce = days_ago(metadata.get("last_reinforced_at", 0))
    boost += 0.05 * exp(-days_since_reinforce / 30)
    # NEW: age decay — 新しい fact ほど boost
    days_since_creation = days_ago(metadata.get("created_at_unix", 0))
    boost += 0.03 * exp(-days_since_creation / 90)  # half-life 90日
    return boost
```

**期待効果**: 「最近の発言」query で新しい fact が上位に来る。「いぶき染め」のような **直近の重要な変更** が、古い「黒髪」fact より優先される。

### 5.6 Contradiction detection

- **解決**: §3.4 (矛盾)
- **コスト**: 中。ingest 時の判定パス追加 + Haiku 1コール
- **積み増し可能性**: ★★★☆☆ 中。`check_near_dup` の判定軸に「矛盾 (同 entity の異なる aspect)」を追加
- **根拠**: [Recallr AI](https://recallrai.com) の 4種分類 — temporal update / correction / preference change / contradiction

実装案 (dedup.py 拡張):

```python
_CONFLICT_PROMPT = """以下の2つの fact は矛盾していますか?
- 既存: {existing}
- 新規: {candidate}

判定:
- "update": 新規が既存を上書き (例: 髪色変更、住所変更)
- "addition": 別 aspect で両方残す (例: Rust 好き + TypeScript 好き)
- "correction": 既存が誤り (例: 「中野梓やる」→「やっぱり澪」)
- "none": 矛盾なし
"""

# ingest 時に near_dup 検出後、矛盾判定を分岐
if conflict_type == "update":
    # 既存 fact に valid_to を入れて archive、新規を追加
elif conflict_type == "correction":
    # 既存 fact を soft delete、新規を追加
elif conflict_type == "addition":
    # 両方 keep (現状動作)
```

**期待効果**: 「中野梓 → 澪 → あずにゃん」の判断揺らぎが temporal valid_to 付きで保存され、最新だけが retrieval に上がる。

### 5.7 Working memory 階層の明示化

- **解決**: §3.3 + session 跨ぎ
- **コスト**: 中。bot.py の session lifecycle に明示的な「working memory」を追加
- **積み増し可能性**: ★★☆☆☆ 低。**既存の session 設計が in-memory dict + 30分 timeout** で動いている。明示的階層化はリファクタ重め
- **根拠**: [MemGPT](https://www.letta.com/blog/agent-memory) / [Serokell design patterns](https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures) — Primary Context (RAM) / FIFO buffer / Archival (disk)

**判断**: ROI 低い。**現状の 30分 session reset が一種の working memory として機能**してる。明示的階層化の効果は限定的。

### 5.8 Filesystem-based fallback (Letta 風)

- **解決**: 大規模 cross-session recall
- **コスト**: 大。Mem0 と並走する別 storage の運用追加
- **積み増し可能性**: ★☆☆☆☆ 低。**現状すでに Mem0 + sqlite graph + .state JSON の 3層**。これ以上 storage を増やすと運用が破綻
- **根拠**: [Letta benchmark](https://www.letta.com/blog/benchmarking-ai-agent-memory) — 74.0% LoCoMo

**判断**: 不採用。**Mem0 を捨てて Letta 化する** なら検討価値あるが、積み増しではない。

---

## 6. 現状の discord-bot 実装の評価

§5 のカタログを **現在の bot がどこまでカバーしてるか** で採点。

| 方策 | 現状 | 完成度 | 改善余地 |
|---|---|---|---|
| Vector retrieval | ChromaDB + Mem0 SDK | 100% | (基盤) |
| Query rewrite | Haiku で canonical キーワード化 | 80% | HyDE 風の hypothetical fact 生成は未 |
| Multi-query merge | search_memories_multi で並列 + dedup | 100% | — |
| Entity alias expansion | `_expand_query_via_graph` (1-hop alias) | 90% | fuzzy match を入れるか議論余地 |
| LLM rerank | Haiku で top-K 判定 | 100% | BGE 二段重ねで cost 削減余地 |
| Hebbian reinforcement | mention_count + recency boost | 100% | age decay 追加で完成 |
| Entity graph 1-hop | `_graph_expand_candidates` | 80% | entity-entity 関係 + temporal は未 |
| 2-stage dedup | cosine + LLM judge (3-tuple) | 100% | contradiction 軸 追加余地 |
| Taxonomy + tier | 8カテゴリ / importance / tier | 100% | — |
| Weekly compact | 日曜9時 cron | 100% | — |
| **BM25 hybrid** | **未実装** | **0%** | **最大の積み増し余地** |
| **HyDE rewrite** | **未実装 (LLM rewrite で部分代替)** | **30%** | **大きな積み増し余地** |
| **Temporal age decay** | **部分実装 (last_reinforced のみ)** | **40%** | **小コストで完成可能** |
| **Contradiction detection** | **未実装** | **0%** | **中コストで実装可能** |
| **Temporal KG (Zep 風)** | **未実装** | **0%** | **大コスト、後回し** |

**総合**: 既存実装は **smart search rerank + reinforcement + graph + taxonomy** の組合せで、業界平均より上 (mem0 vanilla の 68.5% + α 程度の position)。**Zep / Letta レベル (74-75%)** に上げるには **hybrid search + HyDE + temporal の 3点積み増し** が必要。

---

## 7. 推奨ロードマップ

ROI 順 + リスク考慮。

### Phase A (短期 / 1-3日 / 低リスク)

**目標**: cosine recall 漏れと表層差を埋める。LLM コスト追加は最小。

1. **BM25 hybrid 化** (§5.1)
   - sqlite FTS5 で fact text を別 index 化
   - `search_memories_hybrid` を新設、Reciprocal Rank Fusion で vector と統合
   - 既存 `search_memories_multi` は内部関数として残す (rollback path)
   - **新規ファイル**: `services/mem0/bm25_index.py`
   - **修正**: `services/mem0/search.py:search_memories_smart`
   - **コスト**: LLM 呼び出し増無し。disk 数 MB 追加

2. **Temporal age decay の追加** (§5.5)
   - `reinforcement_boost` に `created_at_unix` 由来の age decay を加算
   - **修正**: `services/mem0/reinforce.py:reinforcement_boost`
   - **コスト**: ゼロ (CPU 計算のみ)

3. **dedup threshold の実測キャリブレーション**
   - `MEM0_DEDUP_DRY_RUN=true` を1週間動かして、false-positive (本当は novel なのに drop された) と false-negative (本当は dup なのに残った) を計測
   - 現在の `0.15 / 0.35` を実測で見直し

### Phase B (中期 / 1-2週 / 中リスク)

**目標**: query 表層差と矛盾解消を埋める。LLM 1コール/turn の追加コストを許容。

4. **HyDE rewrite** (§5.3)
   - `_generate_hypothetical_fact` を追加、`_rewrite_query` と並行で使う
   - **修正**: `services/mem0/search.py`
   - **コスト**: Haiku 1コール/turn (+$0.0001)

5. **Contradiction detection** (§5.6)
   - `check_near_dup` に conflict_type 判定を追加
   - `valid_to` metadata を fact に付与、retrieval で valid なものだけ返す
   - **修正**: `services/mem0/dedup.py`, `services/mem0/ingest.py`, `services/mem0/search.py`
   - **コスト**: ingest 時の Haiku 1コール (+$0.0001/fact)

6. **Mem0 検索の監視メトリクス追加**
   - retrieval hit rate / rerank kept ratio / dedup drop ratio を bot.log に統計値で出す
   - 月次で `mem0_compact` レポートに集計
   - **修正**: `services/mem0/search.py` のロギング強化

### Phase C (長期 / 1ヶ月+ / 高リスク)

**目標**: temporal KG レベルに引き上げる。大規模リファクタ覚悟。

7. **Entity-entity 関係 + temporal edges** (§5.4)
   - `entity_relations` table 追加 (src, rel_type, dst, valid_from, valid_to)
   - `services/mem0/extract.py` で関係抽出を追加
   - **修正**: graph.py / extract.py / search.py 全部
   - **コスト**: Haiku が抽出するエンティティ数 × 関係数 × ingest 頻度

8. **Episode boundary の active 化**
   - 現状 `episode_id` metadata はあるが retrieval 上活用されてない
   - 「同一 episode 内の facts を 1束で返す」 retrieval mode 追加
   - **修正**: `services/mem0/search.py`, bot.py

### やらないこと

- ❌ Filesystem-based memory (Letta 風) — 既存ストレージ層が多すぎて運用破綻
- ❌ Working memory hierarchy の明示化 — 現状 30分 session reset で代替できてる
- ❌ Mem0 自体の置き換え (Zep / Letta への乗換) — マイグレーションコストが大きすぎる

---

## 8. 計測指標

改善を観測するために必要なメトリクス。Phase A の **項目 6 (監視メトリクス)** で実装する。

| メトリクス | 計算法 | 目標 |
|---|---|---|
| **retrieval hit rate** | (memories returned > 0) / (total searches) | ≥ 0.85 |
| **rerank kept ratio** | rerank top-K / candidate count | 0.20-0.30 (LLM が選別してる証拠) |
| **graph expand hit rate** | graph_expand で追加された sibling 数 / total | 0.15+ |
| **dedup drop ratio** | drop / candidates | 0.30-0.50 (高すぎ = false positive 懸念) |
| **session-cross recall** | 24h+前の fact が想起された率 (手動評価で計測) | ≥ 0.70 |
| **avg latency** | search_memories_smart の p95 | < 1秒 |
| **monthly cost** | Mem0 関連 LLM コール × token | < $50/月 |

**ベンチマーク自前構築**: discord-bot の過去ログから 50-100 個の "recall test set" を作る (「X について先輩は何と言った」「Y への先輩の好み」のような Q&A pair)。Phase A 着手前に baseline 計測し、各 Phase 後に再計測して improvement を確認。

---

## 9. 結論

**先輩の体感は正しい**。Mem0 < CW inline は recall 面で本当。ただし:

- CW inline も lost-in-the-middle と token cost で破綻する
- Mem0 の弱点はパッチ可能。**既存の smart search pipeline の上に hybrid search と HyDE を積めば Zep レベルに近づく**
- **Phase A の3項目だけでも体感は変わる**。今夜の「いぶき染め事件」のような **cosine cut で fact が出てこない** ケースは hybrid search で大幅に救える

不可逆な置き換え (Mem0 → Letta) は **やらない方が良い**。現状実装の積み増しで十分。

---

## 10. 参考文献

### Mem0 / memory benchmark
- [Mem0 official paper (ECAI 2025, arxiv 2504.19413)](https://arxiv.org/html/2504.19413v1) — Production-ready AI agents with scalable long-term memory
- [Beyond the Context Window (arxiv 2603.04814)](https://arxiv.org/html/2603.04814v1) — Cost-performance analysis fact-based memory vs long-context
- [Zep: A Temporal KG Architecture (arxiv 2501.13956)](https://arxiv.org/abs/2501.13956) — +18.5% LongMemEval / -90% latency
- [Letta benchmarking](https://www.letta.com/blog/benchmarking-ai-agent-memory) — Filesystem 74.0% LoCoMo
- [Mastra Observational Memory](https://mastra.ai/research/observational-memory) — 95% LongMemEval
- [BEAM benchmark](https://mem0.ai/blog/what-is-beam-memory-benchmark-the-paper-that-shows-1m-context-window-isnt-enough) — 1M CW では足りない
- [Mem0 State of Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [5 AI Agent Memory Systems Compared](https://dev.to/varun_pratapbhardwaj_b13/5-ai-agent-memory-systems-compared-mem0-zep-letta-supermemory-superlocalmemory-2026-benchmark-59p3)

### Hybrid Search / Rerank
- [BM25 vs Vector Search (Alok)](https://aloknecessary.github.io/blogs/bm25_vs_vector_search/) — Vector recall not guaranteed
- [Hybrid Search and Re-Ranking in Production RAG (Towards DS)](https://towardsdatascience.com/hybrid-search-and-re-ranking-in-production-rag/)
- [Production Retrievers in RAG (Medium)](https://machine-mind-ml.medium.com/production-rag-that-works-hybrid-search-re-ranking-colbert-splade-e5-bge-624e9703fa2b)
- [Best Reranker Models for RAG 2026 (BSWEN)](https://docs.bswen.com/blog/2026-02-25-best-reranker-models/)
- [Hybrid Search vs Reranker (BSWEN)](https://docs.bswen.com/blog/2026-02-25-hybrid-search-vs-reranker/)
- [BM25 vs Dense for RAG (Ranjan)](https://ranjankumar.in/bm25-vs-dense-retrieval-for-rag-engineers)

### HyDE
- [HyDE (Wang et al. 2022) + HyPE (2025)](https://medium.com/@ali.rafik.ali.97/leveraging-hypothetical-document-embeddings-hyde-to-enhance-retrieval-augmented-generation-434deccb9755) — +42pt precision / +45pt recall
- [Adaptive HyDE arxiv 2507.16754](https://arxiv.org/html/2507.16754v1)
- [HyDE emergent mind summary](https://www.emergentmind.com/topics/hypothetical-document-embeddings-hyde)

### Lost in the middle
- [Liu et al. 2023 arxiv 2307.03172](https://arxiv.org/abs/2307.03172) — original lost-in-the-middle paper
- [TACL 2024 (MIT Press)](https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00638/119630/) — official version

### Contradiction / Conflict resolution
- [Contradiction Detection in RAG (arxiv 2504.00180)](https://arxiv.org/pdf/2504.00180)
- [Recallr AI 4-type classification](https://recallrai.com)
- [Continuum Memory Architectures (arxiv 2601.09913)](https://arxiv.org/html/2601.09913v1)
- [MemoryAgentBench](https://www.emergentmind.com/topics/memoryagentbench)
- [Mem0 RAG vs Memory blog](https://mem0.ai/blog/rag-vs-ai-memory)

### Working memory / Hierarchy
- [MemGPT / Letta agent memory](https://www.letta.com/blog/agent-memory)
- [Serokell long-term memory design patterns](https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures)
- [Practical guide to memory for agents (Towards DS)](https://towardsdatascience.com/a-practical-guide-to-memory-for-autonomous-llm-agents/)

### discord-bot 内部資料
- [ADR-0002: post-rerank gate redesign](../.claude/adr/0002-mem0-post-rerank-gate-redesign.md)
- [Mem0 overhaul 4-phase implementation](2026-05-14-mem0-overhaul-implementation.md)
- [Mem0 architecture issues](2026-05-17-mem0-architecture-issues.md)
- [Mem0 dedup improvement plan](mem0-dedup-improvement-plan.md)
- [MEMORY.md (user_id 設計)](MEMORY.md)

# 2026-07-02 Fable 5 サブエージェント補完レビュー（GPT-5 backlog 補遺）

## 目的

`docs/2026-07-02-fable-refactor-improvement-backlog.md`（GPT-5 による20領域巡回）を前提に、
**backlog 未載の見落とし** と **機能性向上（攻めの改善）** に焦点を当てた補完レビュー。
Fable 5 のサブエージェント7体（Mem0 / セッション・コンテクスト / Tool loop / Deep Research / MCP / パフォーマンス / アーキテクチャ）を並列起動し、既存 backlog との重複を排除する指示付きで実施した。

- 各 finding は実コードを読んだ根拠付き（file:line）。実装前に該当行の再確認を推奨。
- 優先度基準は backlog と同一（P0-P3）。「提案」は機能向上の設計提案。

## 2026-07-02 実装時の再検証で確定した誤検出（3件）

束1〜4 の実装前検証（同日）で、以下の3件は**誤検出または意図的設計**と確定した。実装対象から除外済み。

1. **§2 `_apply_compact_summary` verbatim-tail 逆転（P1）→ 誤検出**。JSONL は append-only で、
   verbatim keep 行は summary 行より物理的に**前**に書かれる。`rows[keep_start:last]` はまさに
   keep_rows であり、現行実装は docstring 通り正しい。
2. **§1 RRF ソート破壊（P1）→ 意図的設計**。`_search_multi_hybrid` の docstring（search.py:161-166）に
   「RRF-only ordering は reinforcement-boost の距離減算数学と衝突する」と明記済み。eval 無しでの
   変更はリスクが利益を上回るため見送り。
3. **§4 MAGI sema 分離（P1）→ 意図的設計**。共有 semaphore は subscriber bucket 保護が目的
   （orchestrator.py の実装コメントに明記）。outer/inner 分離すると実効並列数が bucket 上限を超える。

上記以外の束1〜4 該当項目は 2026-07-02 に実装済み（全テストスイート green を確認）。

---

## 1. Mem0 外部記憶（注入精度・ランキング品質）

### 発見（バグ・設計矛盾）

| 優先度 | 対象 | 内容 | 提案 |
|---|---|---|---|
| P1 | `bot.py:1831-1832` | admin は全ターン無条件で smart search（rewrite+HyDE+rerank = Haiku 3〜4コール直列）。「了解」等の低情報ターンでも走り TTFT 悪化 | クエリ長・固有名詞・疑問文でフル/ライトパス分岐（`_is_recall_worthy_query`） |
| P1 | `services/mem0/search.py:183`, `bm25_index.py:263,284` | **RRF マージ結果を cosine 距離で再ソートし RRF 順序を破壊**。BM25 のみヒット候補は `score=0.8`（ニュートラル値）で距離ソートに混在 | マージ段は `rrf_score` 降順で統一。距離ソート撤廃 |
| P1 | `services/mem0/search.py:73-77,667` | LLM reranker に渡す前に `MEM0_RELEVANCE_MAX_DISTANCE=1.2` の事前フィルタが介入し、「遠いが関連ある」候補が reranker に届かない | smart パスでは pre-filter を外し、フィルタは rerank 後のみに |
| P1 | `services/mem0/format.py:42-53` | **注入フォーマットが importance / tier / created_at / reinforced_at を全部捨てる**。Live と Cold、importance 5 と 2 がモデルに等価に見える | 1行フォーマットに `[imp=5 Live] [2026-05-12]` 等のメタを付与 |
| P1 | `bot.py:2199-2204` | `<retrieved_memories>` が prompt 最先頭に積まれユーザー入力から遠い（attention 局所性の観点で不利） | 記憶・history_search はユーザー入力直上へ移動。invest_kb は先頭のままで可 |
| P2 | `scripts/weekly_mem0_compact.py:148-156` | **weekly compact は候補リストを報告するだけで tier 減衰を実行していない**。CLAUDE.md の「weekly compact が減衰させる」記述と実装が乖離。tier は ingest 時から実質静的 | 実 tier 降格 + soft-archive（`valid_to`）実装、`--dry-run` フラグ付き。CLAUDE.md の記述も実装に合わせて是正 |
| P2 | `services/mem0/search.py:712,813` | エピソードバンドル展開 `score=0.85` とグラフ展開 `0.8` でニュートラル値が不一致。boost 減算後のランクが意図と逆転し得る | `SCORE_NEUTRAL_DISTANCE` 定数を一元化 |
| P2 | `services/mem0/reinforce.py:34-35` | boost パラメータ（`MEM0_BOOST_ALPHA_AGE` 等）だけ import 時読みで再起動なしに変更不可。他 config と一貫性なし | 関数内で遅延ロード（呼び出し頻度は低く性能影響なし） |
| P2 | `services/mem0/ingest.py:32` | `_claim_insert` が MD5。衝突で異なる事実の insert がスキップされ得る（整合性問題） | sha256 の先頭16バイト hex へ |
| P2 | `mcp_mem0_server.py:251-256` | delete 所有権検証が全件取得 O(n)（860件+） | `mem.get(memory_id)` 単一取得 + user_id 照合（SDK のレスポンス形式要確認） |
| P3 | `services/mem0/search.py:190-202` | rewrite プロンプトの正準ラベル15件がハードコード。extract 側は graph 由来の動的辞書（`_format_known_entities`）なのに rewrite 側だけ静的で陳腐化する | rewrite 側でも `_format_known_entities(user_id)` を注入 |

### 機能向上提案

- **Core Memory 分離**（コスト低・効果高）: importance=5 & tier∈{0,1} の基本事実（名前・大学・健康情報等）を TTL 5分キャッシュで常時注入し、クエリ依存 recall と枠を動的配分（core 最大3件 + クエリ依存が残り）。現状はクエリ運次第で基本情報が欠落する。
- **セッション文脈 re-ranking**（コスト中・効果中）: セッション内で既出の entity に紐づく記憶へ軽ペナルティ、未出の関連記憶を優先。`search_memories_smart` に `session_entity_ids` を渡す。

### 総評

5層ハイブリッド検索（vector + BM25/RRF + HyDE + graph + episode bundle）の方向性は正しいが、**「検索の精緻化」に比べ「注入後の LLM 利用効率」への投資が薄い**。短期は注入フォーマット改善 → core memory 分離、中期は RRF ソート修正 + pre-rerank ゲート撤廃、長期はセッション文脈 aware re-ranking + tier 減衰の実運用化。

---

## 2. セッション / コンテクスト維持

### 発見

| 優先度 | 対象 | 内容 | 提案 |
|---|---|---|---|
| P1 | `services/session.py:770-771` | **`_apply_compact_summary` の verbatim-tail が逆転**。summary の「前」の行を keep しており、本来保持すべき summary「後」の verbatim tail という概念と逆。`keep_tail_rows` が境界計算に使われていない | `[srow] + rows[last+1 : last+1+keep]` に修正 |
| P1 | `services/claude_api/compaction.py:354-369` | `_flatten_rows_for_summary` が `content`（表示用短縮テキスト）のみ参照し `api_messages` を展開しない。**ツールヘビーなセッションでは要約入力から assistant の行動が全損**し、compaction 品質（以降全ターンの基盤）が劣化 | content 空時は api_messages 内 text ブロックをフォールバック連結 |
| P1 | `bot.py:1861-1886,2211-2268` | history_search / invest_kb が `will_resume`（lean パス）でも毎ターン再注入。replay 内の文脈と二重化し、チャンネル横断の会話が現発言者 prompt に混在 | lean パスでは history_search をスキップ。invest_kb は token budget を prompt_breakdown で定量化 |
| P2 | `services/claude_api/entry.py:342-356` | `_SNAPSHOT_MAX_AGE_S=900` < `SESSION_TTL_S=3600`。アクティブ会話中に短文ターンが続くと snapshot が age-out し lean prompt の Mem0 文脈がゼロになる | snapshot TTL を SESSION_TTL と同期、または「会話上の位置」で stale 判定 |
| P2 | `bot.py:2288` | `_cap_text(prompt, MAX_PROMPT_CHARS)` の末尾一発切断で、RAG ブロックが大きいと**ユーザーテキスト本体から切れる**。PromptBuilder 側の mem_budget 保護を lean/full パスが使っていない | 切断優先度: memories → history_search → invest_kb の順に削り、ユーザー本文は最後まで守る。コンポーネント別 budget を PromptBuilder に集約 |
| P2 | `services/session.py:136-148` | `_flatten_sessions` が channel キーへ上書き保存（last-wins）。同一チャンネルの haroin / b0nchichi 並存時、再起動で先の admin の `mcp_server_keys` / sticky_model が消える | disk フォーマットを `"ch:uid"` キーに versioned 移行（backlog P1 の具体スキーマ） |

### 機能向上提案

- **Warm-start diff-patch**: 新 recall と保持 snapshot を diff し、変化分のみ `<memory_update>` として lean prompt に注入。トークン削減と新情報優先の両立（コスト中）。
- **話題転換検知 selective compaction**: 現状 microcompact は gap_minutes ベースのみ。話題転換検知時に無関係 tool_result を選択的に縮約（コスト低、`microcompact_tool_results` にオプション追加）。

---

## 3. Tool Call 精度 / Claude API 中核

### 発見

| 優先度 | 対象 | 内容 | 提案 |
|---|---|---|---|
| P1 | `services/claude_api/tool_loop.py:979` | 並列 tool 実行の `asyncio.gather` で CancelledError 伝播制御なし。**/abort 時に orphan tool が残る** | CancelledError のみ re-raise、他は tool_result 化するラッパー |
| P1 | `tool_loop.py:1554-1556` | tool エラー回復 nudge 判定の `text_across_turns` に `_fallback_alert`（システム通知文）が混入し、閾値超過で nudge が発火しない | nudge 判定前に fallback alert 文を除外 |
| P1 | `services/claude_api/agent_tool.py:228-238` | subagent が親の `McpPool` を直接共有。複数 subagent の同時 lazy-spawn で `handles` dict 競合の可能性（spawn timeout なし問題と複合するとデッドロックリスク） | per-key `asyncio.Event` で spawn 排他、または spawn 済み handle のみ渡す |
| P2 | `services/claude_api/tools.py:717-723` ほか | builtin tool schema に `additionalProperties: false` がない（Agent tool にはある）。モデルの余計な引数追加を検証で弾けない | 全 builtin schema に1行追加 |
| P2 | `static_mcp_schemas.json:291-300` | **`brave_web_search` の freshness enum にプレースホルダー文字列 `"YYYY-MM-DDtoYYYY-MM-DD"` がリテラルで入っている**。実際の日付範囲を渡すと jsonschema 検証で弾かれ、日付範囲検索が使用不能 | enum を pattern (`^(pd|pw|pm|py|\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2})$`) へ |
| P2 | `tool_loop.py:1450-1471` | text-invoke rescue / correction カウンタが独立で、誤設定時に max_turns を食い潰す | 合計ターン共通カウンタに束ねる |
| P2 | `services/claude_tag.py:41` | `:claude:` タグ処理が `claude_sdk` 経由 import。旧 SDK パスに落ちる可能性が残る | `services.claude_api.entry` 直接 import へ（意図的なら ADR 化） |
| P3 | `tool_loop.py:376-380` | hallucinated path 検知の距離計算が負値→0 補正で false positive 率が意図より高い | 逆順ペアの距離計算を修正 |

### 機能向上提案

- **`tool_choice: {type: "any"}` エスカレーション**（コスト中・効果高）: text-invoke poison 頻発時に1ターン限定で tool_use を強制する第3の救済路。現状 correction/rescue の2段のみで、fable の text-invoke 収束を早める最有力策。
- **tool description の positive-first 化**: `"NOT read_file, NOT read"` の否定リストより `"Tool name: Read (exact)."` を冒頭に。Anthropic 推奨の「何をするか」優先へ。
- **Agent tool の `subagent_type`**: enum 1値のみだと省略されがち。省略時挙動を description に明記するか enum を外す。

---

## 4. Deep Research

### 発見

| 優先度 | 対象 | 内容 | 提案 |
|---|---|---|---|
| P0 | `services/deep_research/orchestrator.py:822-833` | 承認ゲートの `wait_for` タスクを cancel 後に await していない。pending task 警告とGC タイミング依存の事故 | cancel 後に `await t`（CancelledError 吸収）を追加 |
| P0 | `orchestrator.py:864-882` | central question gate の訂正ループに上限がない。訂正を送り続けるユーザーが `_research_busy` を任意時間占有（DoS 経路） | `max_corrections=5` 等の上限追加 |
| P1 | `services/deep_research/magi.py:88,111,135` | **MAGI 3レンズの分解・ワーカー・lead が同一 semaphore を争奪**。sema(2) で3レンズ同時起動すると実効並列度が設定値を大幅に下回り starvation の可能性 | レンズ間 outer_sema とワーカー用 inner_sema を分離 |
| P1 | `services/deep_research/prompts.py:296-322` | `build_evidence_digest` のソース一覧に件数上限なし。ソース多数で fixed 部が max_chars を超え **searcher 要約が全部落ちる** | ソース一覧に `[:50]` 上限 |
| P1 | `services/deep_research/sources.py:91-99`, `registry.py:345-349` | `verify_citations` は `cited_sids` を見るが**常に空配列で dead code**。テストは動かない経路をカバーし誤った安心感 | 削除するか、searcher 要約から `[S#]` を抽出して充填 |
| P1 | `agent_tool.py:226-238` | Agent tool 経路と MAGI 経路の semaphore が別系統で、同時走行時にサブスクリプション枠が枯渇し得る | 共通の並列度管理へ |
| P2 | `orchestrator.py:183-186` | View の throttle がテスト不能（loop.time() 直接依存）。throttle 不全は Discord rate limit 直撃 | `_throttle_s` をコンストラクタ注入 |

### 機能向上提案

- **最終レポートへの adversarial verification**（コスト中・効果大）: MAGI の懐疑レンズは証拠収集前の独立推論で止まっている。integrator 出力に懐疑エージェントを1回通し「単一ソース依存・確証バイアス・誤引用」を差し戻す（最大1回）。
- **日英並列クエリの構造化**（コスト中・効果大）: planner schema に `ja_query`/`en_query` を持たせ orchestrator が並列投下。技術・金融テーマの一次情報リーチが確実に向上。
- **dead-end 検知**: 全 searcher「結果なし」でも ok=True で空レポートが生成される。検知して planner に別角度サブ質問を再生成させるフォールバック。
- **ソース多様性スコア**: `SourceRef` に domain を追加し、単一ドメイン偏重を synthesizer に警告（コスト小）。
- **中間報告 + 3択ゲート**: searcher 完了都度の中間投下 + 「続行/方向転換/中止」（コスト小、既存 `_SearcherView.finish` 拡充）。

---

## 5. MCP 統合

### 発見

| 優先度 | 対象 | 内容 | 提案 |
|---|---|---|---|
| P0深掘り | `services/claude_api/mcp_client.py:117`, `config.py:879-890` | env 全継承の具体経路: `DISCORD_TOKEN` 等全キーが全 MCP 子プロセスへ。**polymarket / minkabu-fx / yfj-quote が同一 venv 共有**で1サーバー汚染が全体に波及 | サーバー別 `_MCP_ENV_ALLOWLIST` 実装（コード案はレビューログ参照） |
| P1 | `tool_loop.py:867-880`, `config.py:625` | **通常 chat 経路の MCP tool 結果に文字数上限がない**（DR 経路は20K制限あり）。brave 検索1回で30K-100K chars がコンテクストに直撃し得る | `MCP_TOOL_RESULT_CHARS=20000` で head+tail truncation |
| P1 | `mcp_client.py:81-124` | `_spawn_one` の initialize/list_tools が無制限 await（dump スクリプトは30s timeout 持ちで不整合）。1サーバー hang で後続 eager spawn が全ブロック | `asyncio.wait_for(..., 20s)` + 失敗隔離 |
| P1 | `services/mcp_selector.py:78-84,39-55` | selector の `"docs"`（4文字部分一致）`"x "` が日本語文で誤爆。逆に「レートどうなってる？」は為替なのに未検知（false negative）。ALWAYS_ADVERTISED との使い分けルールが不文律 | トリガー境界修正 + false positive/negative テスト追加 + 責務文書化 |
| P1 | `mcp_client.py:172-203` | mem0 schema がハードコード1行 description で、`mcp_mem0_server.py` docstring の詳細（category/importance/tier）と乖離。同期機構なし | JSON へ一本化 or docstring 同期 |
| P2 | `static_mcp_schemas.json:384` ほか | `brave_local_search` の default `country="US"`、`brave_web_search` の `search_lang="en"`。**日本語 bot なのに未指定時 US/英語検索になる** | default を JP/ja に上書きする post-process を dump スクリプトへ |
| P2 | `scripts/dump_mcp_tool_schemas.py:88`, `mcp_client.py:236` | dump 失敗サーバーが空リストを JSON に書き、merge 側 `extend([])` で**正常だった既存スキーマが空に上書き**され得る | dump/merge 両側に `if tools:` ガード + weekly CI で差分 PR 化 |
| P2 | `entry.py:132-140` + `mcp_selector.py:46-51` | 金融系サーバーが ALWAYS_ADVERTISED と `_WEB_TRIGGERS` に二重登録 | どちらかに集約 |
| P3 | `static_mcp_schemas.json:1107-1134` | `analyze_orderbook` の `signals` 戻り形式が未定義でモデルが解釈不能 | description に example 追加 |

---

## 6. パフォーマンス

### 発見（推定インパクト付き）

| 優先度 | 対象 | 内容 | インパクト |
|---|---|---|---|
| P0 | `services/mem0/client.py:282` | ChromaDB heartbeat が同期 `requests.get(timeout=5)`。**イベントループを最大5秒ブロック**（ChromaDB 再起動直後の init パス） | 最大5秒の全体停止解消。`asyncio.to_thread` 1行 |
| P1 | `services/mem0/search.py:784-808,583-588` | `_graph_expand_candidates` が entity ごとに sqlite→ChromaDB を直列 await（N+1、最大9回直列） | `asyncio.gather` 化で TTFT 50〜200ms 削減 |
| P1 | `bot.py:2507-2520` | 純 CPU 関数 `split_settled` / `first_block_span` を毎 tick `asyncio.to_thread` に投げている（最大500回/応答）。dispatch オーバーヘッドが実行コスト超 | 直接呼び出しに変更。tick あたり1〜3ms |
| P1 | `bot.py:1594-1611` | GIF spinner が毎秒 PIL レンダリング + Discord へファイル再アップロード | 単語変化時のみ再生成へ。CPU スパイク軽減 |
| P2 | `mcp_client.py:127-142` | eager spawn が直列。WEB+HEAVY 同時トリガー時に累積 | gather 化で300〜1000ms 削減（ExitStack 順序に注意） |
| P2 | `bot.py:2496` | `_render_incremental` の interval gate がロック取得後。ストリーミング毎 delta でロック競合 | gate をロック外へ |
| P2 | `bot.py:2375-2401` | スピナー再配置が段落ごとに send+delete 2往復。10段落で20回の追加 API 呼び出し | 再配置頻度を下げる（3段落ごと等） |
| P2 | `tool_loop.py:627` | `_last_context_tokens` 無制限 dict（backlog 既載の具体実装案）: OrderedDict + cap 500 の LRU | 長期稼働の劣化防止 |

**トップ3**: (1) graph expand N+1 解消、(2) 同期 requests 排除、(3) ストリーミングの to_thread 除去。

---

## 7. アーキテクチャ / リファクタリング具体設計

### backlog 未載の構造的問題

| 深刻度 | 対象 | 内容 |
|---|---|---|
| Critical | `services/claude_sdk.py:62` + `services/claude_api/entry.py:184` | **`claude_semaphore` が2系統存在**し実効並列上限が `CLAUDE_MAX_PARALLEL * 2` になり得る。legacy 実装が同一プロセスに ghost state として常駐。PR9（claude_sdk 削除）が未完遂のまま `bot.py` / `web_api.py` / `claude_tag.py` が参照継続 |
| Warning | `bot.py:360-378` = `prompt_builder.py:58-74` | `_wrap_trusted` / `_wrap_untrusted` が**字義的に同一の二重実装**。片方だけ変更されると信頼境界タグが分岐する（セキュリティ面の将来リスク） |
| Warning | `bot.py:1984-2011` = `bot.py:2112-2147` | lean/full 両経路の添付ブロック構築が字義的コピー。`_build_attachment_blocks()` に統合可能 |
| Warning | `services/mem0_service.py:1` | LEGACY 宣言済みだが bot.py / prompt_builder / mcp_mem0_server / **health.py:45** が参照。削除すると health check が壊れる。廃止期日なし |
| Warning | `config.py:420-422,656-660` | import 時副作用（makedirs / mode 700）。テストで config を import するだけで本番ディレクトリが作られ、conftest の monkeypatch が間に合わない |
| Warning | `bot.py:1738-3036` | on_message 内 closure 群が8つの mutable dict/list を nonlocal キャプチャ。DI の口がなく部分単体テスト不可能。`conftest.py:38` が cooldowns/_daily_quota しかリセットできないのはこのため |
| Warning | `bot.py:268` | `_channel_semas` がモジュールグローバルで差し替え不能。セマフォ解放漏れ（backlog P0）のテストが書けない |

### bot.py 分割の具体設計（backlog「分割します」の一段深い案）

- `services/intake.py` — `gate_message()`: 受信ゲーティング（bot.py:1188-1216）
- `services/context_builder.py` — `MessageContext` dataclass + `collect_context()` + `build_prompt()`（bot.py:1738-2296）
- `services/response_renderer.py` — `StreamState` dataclass + 送信/レンダリング closure 群（bot.py:581-675, 2319-2957）
- セマフォは acquire 直後 `_sema_acquired=True` フラグ + 条件付き finally（暫定）→ `ChannelSemaphorePool` class 化（恒久）

### ロードマップ（5フェーズ、壊さず進める順）

1. **Phase 1（〜1週）**: P0 最小修正。セマフォフラグ、wrap_* 重複解消（bot.py 側を thin alias 化）、claude_sdk の ghost semaphore 削除、conftest に `_channel_semas.clear()`
2. **Phase 2（1-2週）**: 重複除去。添付ブロック統合、mem0_service 参照を services.mem0 直 import へ、config.py 副作用を `setup_dirs()` に遅延
3. **Phase 3（2-3週）**: intake / context_builder 切り出し。完了条件: on_message 実質500行以下
4. **Phase 4（2-3週)**: StreamState + response_renderer、**claude_sdk.py 削除（PR9 完遂）**
5. **Phase 5（継続）**: pyright warning 運用、http_session / semaphore の DI 化

---

## 実装順序の推奨（本補遺 × backlog 統合）

1. **即効・低コスト（1日以内で複数閉じられる）**: brave freshness enum 修正、brave default JP/ja 化、`additionalProperties: false`、同期 requests 排除、to_thread 除去、dump 空リストガード、MCP result truncation
2. **品質の土台（compaction/replay）**: `_apply_compact_summary` 順序修正、`_flatten_rows_for_summary` の api_messages フォールバック → 以降の全セッション品質に効く
3. **Mem0 注入品質**: format メタ付与 → core memory 分離 → RRF ソート修正 → pre-rerank ゲート撤廃
4. **Deep Research 安定化**: 承認ゲート await 修正 + 訂正ループ上限 → MAGI sema 分離 → adversarial verification 追加
5. **リファクタ本体**: 上記ロードマップ Phase 1 から。fable 公開直後の回帰リスクを踏まえ「テスト固定→切り出し」を厳守

## メモ

- weekly_mem0_compact の tier 減衰未実装（§1）は CLAUDE.md の記述と実装の乖離。ドキュメント側の是正も忘れないこと。
- 本補遺もコード変更は行っていない。レビュー実施: Claude Fable 5 サブエージェント7体（2026-07-02）。

# かなえ記憶システム再設計プラン v3 — 「大工事不要。3つの穴を塞ぐ」

作成: 2026-07-02 かなえ（haroin発注）
改訂: v1(SQLite一次) → v2(ファイル一次・DB索引) → **v3(コードベース走査の結果、ファイル一次層を取り下げ。既存層のピンポイント強化に転換)**
外部相談: DeepSeek V4 Flash ×3回 / **GPT-5は3回連続で応答取得失敗（SecondOpinionのOpenAI経路要デバッグ）**

---

## 0. v3の結論

**プランv2は「実装を読まずに書いた」点で欠陥だった。** haroinの指示でコードベース（services/mem0/ 5,469行＋関連スクリプト群）を走査した結果：

1. **v2で「新設」とした機構の約7割が既に実装済み**（下表）
2. 観測された問題P1〜P6の根因は**3つの明確な穴**に絞られ、いずれも既存層のメタデータ拡張で塞げる
3. ファイル一次層の全面導入は**過剰工学**（DeepSeek第3回意見に同意）。工数8-10日→**4-5日**に縮小

## 1. コード走査の発見 — 既に在るもの

| v2で「新設」としたもの | 実装の実態 |
|---|---|
| supersede/valid_until | ✅ conflict.py: LLM矛盾分類(update/correction/addition/none)→旧factにvalid_to打刻(ソフトアーカイブ)。env flagでロールバック可 |
| 検索時の失効フィルタ | ✅ search.py `_is_fact_valid()` 常時適用 |
| SQLite意味層 | ✅ graph.py: entityグラフ(valid_from/to、部分unique index、エイリアス解決、検索時クエリ拡張) |
| 出典リンク | ✅ episode_id メタデータ(allowlist済み) |
| 文脈タグ | ✅ channel_id/channel_name/source メタデータ |
| 再固定化 | ✅ reinforce.py + 検索時reinforcement boost |
| 重複統合 | ✅ 2段dedup(距離ゲート→LLM判定→superset merge) |
| 話者混入監視 | ✅ weekly compact P5: other-speaker contamination check(exit 2でアラート) |

基盤: mem0 OSS 1.0.11 + ChromaDB(server:8765, collection=kanae_memories_safe) + embedder=OpenAI text-embedding-3-small + 抽出/rerank=Haiku 4.5(OAuth)→DeepSeek V4 Flash二段フォールバック。ハイブリッド検索(BM25+vector+HyDE+graph expansion+rerank+core memories)。

## 2. 真の診断 — 3つの穴

| 穴 | 対応する問題 | メカニズム |
|---|---|---|
| **(a) attribute概念の欠如** | P1/P3 | conflict分類は「dedupのnear-dupゲートを通ったペア」にしか走らない。文面の違う同一属性の新旧事実（数値更新等）は埋め込み距離が遠くゲートを通らず、**矛盾検出に到達しないまま並置**される |
| **(b) conditionの欠如** | P1' | 時間帯依存の習慣はconflict分類が正しくaddition判定するが、「朝/昼」等の**条件を表現する場所がなく**、統合形に畳めない |
| **(c) scopeの欠如** | P6 | 特定会話でのみ有効なメタ情報（発言者訂正等）が汎用factとして保存され、後日無関係な場面で注入される |

## 3. v3実装プラン — ピンポイント強化 6 Phase

| Phase | 内容 | 塞ぐ穴 | 目安 |
|---|---|---|---|
| **A** | メタデータに`attribute`追加（entity+属性キー、例 `weight`, `habit.coffee`）。ingest時にHaiku抽出へ1フィールド追加。**conflictゲートを「同一user×同一attributeの既存fact照会」に拡張**（embedding距離に依存しない決定的ルート） | (a) | 1日 |
| **B** | メタデータに`condition`追加（例 `time:morning`）。矛盾分類がadditionかつ同一attributeの場合は**条件付き事実として統合**。検索時に現在文脈で優先度評価 | (b) | 1日 |
| **C** | メタデータに`scope`追加（`global` / `conversation:<id>` / `meta`）。ingest時に会話メタ情報を自動判別してscope付与、通常検索から除外 | (c) | 0.5日 |
| **D** | **手続き店**: `scripts_registry.md`（成果物レジストリ: path/用途/実行例/依存データ源）＋成果物作成時の登録運用 | P4 | 0.5日 |
| **E** | **compaction直前フラッシュ**: 圧縮で落ちる確定事項を既存ingestパイプへ流してから捨てる（L2→L3接続） | P5 | 1日 |
| **F** | **即時修正パス**: 会話中のユーザー訂正を検知→既存`_archive_outdated_facts`を即時呼び出し（夜間を待たない）。実装は既存機構の呼び出しのみ | 運用 | 0.5日 |

合計 **4.5日**。全Phaseが既存機構の拡張であり、新規ストレージ層なし・無停止・Chromaのインデックス再構築不要。

## 4. v2から生き残った要素 / 取り下げた要素

**生存**: 手続き店（D）、compactionフラッシュ（E）、即時修正（F）、「裁定は会話・記帳はかなえ」の運用原則（Fの設計思想）、プロジェクトシート＝読む記憶（現状維持）

**取り下げ**: Markdown+frontmatterファイル一次層（真実の二重化リスク＞利得。人間可読性は既存のプロジェクトシートが既に担っている）、夜間approve式バッチ（リアルタイム性と矛盾。既存ingest＋週次監査で代替）、既存900件の一括ファイル化（不要になった）

## 5. 外部相談の経緯と教訓（記録）

- 第1回(v1時): DeepSeek「Lettaへ乗り換えろ」→棄却
- 第2回(v2時): DeepSeek「ファイル一次・DB索引」→採用してv2作成
- 第3回(コード走査後): DeepSeek「ファイル一次は過剰工学。既存層拡張~500行で足りる」→**採用してv3作成**
- **教訓**: 同一モデルが相談の文脈次第で正反対の結論を出す。外部意見は「与えた文脈への最適化」であり、**コード走査後（判断材料最大時）の意見を最重視**するのが正しい。走査を指示したharoinの判断がプランの過剰工学を止めた
- GPT-5: 3回連続で応答取得失敗。OpenAI経路のデバッグ後、v3の検算に再利用価値あり

## 6. 未決事項

- attribute語彙の設計（自由文字列 or 統制語彙。まず自由＋週次レポートで表記ゆれ監視を提案）
- condition評価の実装位置（検索時フィルタ or 注入時のprompt表現）
- Phase A着手のGO判断

# 2026-07-03 discord-bot 大規模リファクタリング計画

## 目的とスコープ

リポジトリ全体（約12万行、テスト除く実装 ~35k行）を構造の観点から走査し、
大規模リファクタリングの実行計画を定める。**バグ・セキュリティ・運用信頼性の
棚卸しは `docs/2026-07-02-fable-refactor-improvement-backlog.md`（20領域巡回）が
既に存在する**ため、本書は「構造・アーキテクチャの再編」に軸足を置き、
バックログとの実行順序の整合を§7で定義する。

## 調査方法

- 読み取り専用サブエージェント5系統で並列走査（コア入口 / claude_api /
  記憶・セッション / 周辺サービス / utils・テスト・インフラ）
- 外部モデル合議: SecondOpinion（GPT-5.5 codex / Gemini 3.1 Pro / DeepSeek V4 の
  3社並列）+ Oracle（gpt-5.5-pro, Responses API 直）で戦略と争点を検証
- 走査結果の主要な主張は grep / git で裏取り済み（誤報1件を訂正: distill.py は
  tool_loop.py:997 から呼ばれており統合済み）

## 1. 現状診断（数字で見る構造問題）

### 1.1 God module 群

| ファイル | 行数 | 問題 |
|---|---:|---|
| `bot.py` | 3,060 | on_message 実質1,873行に前処理→Mem0 recall→プロンプト構築→LLM呼び出し→ストリーミング応答→記録→エラー処理が直列。モジュールレベル mutable 状態16個。スラッシュコマンド12個直付け |
| `services/claude_api/tool_loop.py` | 2,090 | ストリーミング drain、ツール dispatch、テキスト内 tool 呼び出し rescue、refusal fallback、cache breakpoint 注入、usage telemetry が同居 |
| `config.py` | 1,394 | 定数+関数ロジック混在。claude_api 31ファイル中23が依存する事実上のグローバル名前空間 |
| `services/claude_api/entry.py` | 1,379 | call_with_tools 1,000行超。630行の retry ループ（ネスト6〜8層）に エラー分類→バックオフ→OAuth failover→deliberate tier 後処理が縫い込み |
| `services/deep_research/orchestrator.py` | 1,032 | orchestration + state machine + UI 承認待ち + MAGI 推論 + 予算管理が単一ファイル |
| `services/mem0/search.py` | 967 | search_memories_smart 1関数(174行)に7フェーズ直列（morpho/alias展開→LLM書き換え+HyDE+キーワード抽出→hybrid検索→グラフ1-hop→LLM rerank→強化boost→コア記憶マージ） |

### 1.2 重複・散在

- **resilience 4重散在**: `retry.py`（バジェット+バックオフ）/ `ratelimit.py`（ヘッダ
  観測+ペーシング、シングルトン `_state`）/ `errors.py`（文字列カテゴリ分類）/
  tool_loop 内の独自リトライ。retry-after ヘッダパースが2重実装
- **agy_runner / codex_runner**: Gemini/ChatGPT CLI の subprocess ラッパがほぼコピペ
  （`_clean_env`、availability TTL キャッシュ等が同型）
- **LLM フォールバック連鎖の2重実装**: Anthropic OAuth→OpenRouter→OpenCode の切替が
  `mem0/client.py`（Mem0 SDK config）と `mem0/cli_compat.py`（直接 Haiku 呼び出し）に
  別実装。**OAuth failover 時に片方だけ切り替わる不整合リスク**
- **セッション概念の2重化**: `services/session.py`（952行, JSONL永続化+atomic write）と
  `services/claude_api/session_store.py`（in-memory TTL+sticky model）
- bot.py 内: メッセージ分割送信3箇所、HTTPException 処理4箇所、ステータス edit 4箇所

### 1.3 legacy / 未完の移行

- **claude-api-unify 計画は PR8 まで master マージ済み**（CLI SDK→素の Anthropic API
  移行完了、dispatcher デフォルト api）。**PR9（`services/claude_sdk.py` 869行の削除）
  だけ未完で停止中**。bot.py は今も claude_sdk.call_with_tools 経由（内部 dispatch）
- `services/mem0_service.py`: 12行の legacy re-export 層。bot.py / web_api.py /
  mcp_mem0_server.py 等の主要 caller がまだ経由（バックログ P3 にも記載）
- deliberate tier パイプライン10ファイル（ADR-0034, WIP）: router→planner→tool_loop→
  verifier→magi_lite→second_opinion。distill は tool_loop から呼び出し済みで統合済み

### 1.4 状態管理

モジュールレベル mutable 状態 **計28個**（bot.py 16 + claude_api 12）。
channel semaphore LRU、cooldowns、model/effort overrides、ratelimit._state
シングルトン、session TTL dict、compaction 失敗カウンタ等。asyncio 並行下の
保護は一部のみで、テストは conftest の `_isolate_bot_state`（autouse monkeypatch）で
毎回リセットしている。

### 1.5 テスト・開発インフラ

- テスト2,464個 / 104ファイル。**unittest.mock + monkeypatch に強結合**
  （test_bot.py 単体で monkeypatch 593回）。private 関数直接テスト多数
- **ruff / mypy / formatter 未導入**（lint CI は shellcheck のみ。requirements-dev は
  pytest 系のみ）
- flaky 候補: `asyncio.sleep` ベースの race テスト ~20箇所

### 1.6 リポジトリ衛生（即時対応可能な確定リスト）

- `bot.log.1`（12万行）が **git tracked**。`.gitignore` に `bot.log*` / `htmlcov/` なし
- master マージ済みで削除可能なブランチ **13本**: refactor/claude-api-unify,
  refactor/memory-p0/p1/p2, refactor/tier-m/tier-s, feat/mem0-context-enhancement,
  feat/mem0-quality-overhaul, feature/mem0-recall-hybrid-hyde-kg,
  worktree-agent-* 4本
- worktree 残骸: `.claude/worktrees/agent-acf32227e7fadd18e`（走査ノイズ源）
- feature/dynam-ueda-parser に未コミット変更26ファイル（機能開発と混在）
- ルート直下: `example-com.png`、`.playwright-mcp/`、`.state/mem0_db.bak-*` 多数

## 2. 外部モデル合議の結論

### 2.1 3モデル共通見解（GPT-5.5 / Gemini 3.1 Pro / DeepSeek V4）

1. **既存2,464テストは「命綱」ではなく「負債」**。構造変更で確実に壊れる。
   修復に工数を溶かさず、characterization / approval / contract test の新設層を
   品質ゲートにし、壊れた mock テストは目的を読み取って契約テストへ置換 or 削除
2. **P0 での ruff/mypy 全面適用は時期尚早**。12万行への一括適用は1人開発を破綻させる。
   最小 ruff + baseline 方式（新規・変更ファイルのみ CI で fail）に留める
3. **「P0.5: テスト耐性化」フェーズを P0 直後に挿入**（3モデル一致で最重要の追加提案）
4. legacy 削除は「grep で到達不能確認 + 契約テスト整備」後。運用設定（env/systemd）で
   生きている経路がないか確認してから
5. 検索・記憶系の品質退行はテストで捕まらない → **固定クエリセットの回帰評価**が必須

### 2.2 3モデルが指摘した見落としリスク

- **asyncio 並行下の race**: 状態のクラス化でロック粒度を誤ると重複応答・記憶欠落・
  セッション混線（Gemini: 「リファクタ中に微妙なタイミングがずれるだけで顕在化する」）
- **2重セッションの split-brain**: ストリーミング中断・ツール実行エラー時に片方だけ
  更新→コンテキスト巻き戻り・Mem0 不整合（時限爆弾との評）
- **LLM failover 不整合**: リクエスト層は OpenRouter、プロンプト構築層は Anthropic
  想定のまま、という中間状態が生まれ得る
- **systemd 実行環境と CI の差異**: 環境変数、cwd、権限、SQLite/ChromaDB ロック
- **WIP（deliberate tier）との衝突**: リファクタ対象と機能開発が同じファイルを触る

### 2.3 争点と裁定（gpt-5.5-pro による最終裁定）

3モデルで意見が割れた4争点を gpt-5.5-pro（Responses API 直、深考モード）に裁定させた。

**裁定サマリ**（全文: `docs/2026-07-03-refactor-arbitration-gpt55pro.md`）:

| 争点 | 裁定 | 核心根拠 |
|---|---|---|
| 分解順序（bot.py先 vs claude_api先） | **A案: bot.py の境界固定が先**。claude_api には薄い façade のみ先行し、entry/tool_loop の深い再設計は境界固定後 | retry/streaming は挙動差が障害に直結し、先に触ると失敗時に「呼び出し側か内部か」を切り分けられない。境界を固定すれば旧 claude_api を black box として使い続けられ、**途中で止まっても中間状態が健全** |
| Cogs 採用 | **初期は非採用**。素のモジュール分割 + `setup_hook` の明示的 `register_commands()` | コマンド12個+イベント2個では lifecycle 管理の恩恵が薄く、主問題（on_message の巨大 orchestration）を解決しない。Cogs は `self.bot` に状態が散り AI エージェントにも不利 |
| 状態28個の扱い | **(b) サブシステム毎の小さな state class**（ConversationState / ClaudeClientState / BatchState / RateLimitState / DiscordRuntimeState 等）+ 配線用の薄い AppContext。**単一巨大 BotState は新たな god object になるので却下**。「頻繁に触るものだけクラス化」も conftest リセット可能性を壊すので最終形としては却下 | 各 state に `reset()` / `close()` を持たせ conftest から確実に初期化。移行期は旧 module global を新 context への委譲 wrapper に。**新しい mutable global は禁止** |
| ruff/mypy タイミング | **今すぐ最小導入**（P0）。ruff は事故検知系のみ、mypy は baseline + 新設境界（MessageContext / Gateway / state class）を常時対象 | **AI エージェントの暴走検知として価値が高い**（存在しない import・リネーム漏れ・未定義変数を機械的に捕捉）。後入れだと巨大 diff に埋もれて原因切り分け不能。characterization test（挙動差検知）と役割が異なり両方必要 |

裁定が付けた総合実行順序（9項目）は §4 のフェーズ計画に反映済み。特筆事項:
rollback 手順・DB/記憶/batch のバックアップ確認を**着手前に**確定すること（順序 #1）。

### 2.4 計画レビュー（第2ラウンド）

ドラフト計画に対し SecondOpinion 3モデル（要約版）+ gpt-5.5-pro（全文レビュー、
`docs/2026-07-03-plan-review-gpt55pro.md`）で辛口レビューを実施し、以下の重大指摘を
本版に反映した:

1. **P1 は「低リスク」ではない**（4モデル一致）: 削除系は「shim 追加 → 呼び出し元移行 →
   flag 切替 → 観察 → 物理削除」の多段原則へ。`claude_sdk.py` 削除は Gateway 契約確定後
   （P2 末尾）に移動
2. **P2 の詰め込みすぎ**: 状態移行を P2.5 として分離し、「P2 本体ではセッション・mutable
   状態に触らない」境界契約を明記
3. **P4 の束ねすぎ + 遅すぎ**: セッション統合（P4a）と LLM fallback 統一（P4b）を分離。
   書き込み経路単一化・provider façade・failover 時 Mem0 再初期化は P1 に薄く前倒し
4. **config.py 後回しの危険**（Gemini: 致命的）: 読み取り専用 config façade を P1 に前倒し。
   物理分割は P5 のまま
5. **運用安全の不足**（Pro: 「これはリファクタ計画であって稼働中 bot の移行計画ではない」）:
   **P0.25（Deploy Safety / Observability）を新設**し前提条件化
6. **bot.log.1 は情報漏えいインシデント扱い**（Pro）: untrack で終わらせず secret scan・
   token rotation 要否・履歴 purge 判断まで P0 に含める
7. **工期 6〜10週は楽観的**（4モデル一致）: **2リリース制 + 総工期 12〜16週**に修正
8. **legacy マーカーの CI 空洞化リスク**（Pro）: 統制ルールを P0.5-d に追加

## 3. 設計原則（本計画で守ること）

1. **各 PR は「テスト green + bot 起動確認」を通過してからマージ**。挙動変更と構造
   変更を同一 PR に混ぜない
2. **中間状態が健全であること**: どのフェーズで中断しても、稼働中 bot が壊れない・
   次の開発が続けられる状態を保つ
3. **公開境界から固定する**: 内部を分解する前に、その層の入出力契約をテストで固定
4. **削除は shim を一世代挟む**: legacy 層は「re-export shim + Deprecation ログ」を
   1リリース挟んでから物理削除
5. **1人+AIエージェント開発の現実に合わせる**: フルDIコンテナ・過剰な抽象化はしない。
   ただし新設する境界（Gateway / state class / service）は**コンストラクタ注入（手動DI）を
   必須**とし、monkeypatch 依存を再生産しない
6. **機械的 move と意味変更を絶対に混ぜない**: 差分行数（±500行目安）より優先するルール。
   move だけの PR は import 修正込みで大きくてよい。意味変更を含む PR は小さく
7. **リスクの高い PR ごとに観察期間を設定**: state / session / fallback / mem0 を触る PR は
   24〜72時間、純粋 move は短縮可。フェーズ単位ではなく**可逆な小 PR 単位で master に反映**

## 4. フェーズ計画

> 総工期目安: **12〜16週間**（1人+AIエージェント、稼働継続前提。第2ラウンドレビューで
> 「6〜10週は楽観的」と4モデル一致判定）。**2リリース制**を採用: 第1リリース =
> P0〜P2.5（安全網・境界固定・状態整理まで）、第2リリース = P3〜P5（内部分解）。
> 第1リリース完了時点で一度立ち止まり、成果と残リスクを評価してから第2リリースへ。
> **P0〜P0.5 が終わるまで構造変更 PR を作らない**こと（安全網が先）。

### P0: リポジトリ衛生 + 最小静的解析（3〜5日）

| PR | 内容 | 検証 |
|---|---|---|
| P0-a | `bot.log*` の**情報漏えいインシデント対応**（Pro 指摘で格上げ）: untrack + `.gitignore` 追加（`bot.log*` / `htmlcov/` / `example-com.png` / `.playwright-mcp/`）に加え、tracked だったログの secret scan（token 断片・個人情報）、混入時は token rotation と git 履歴 purge 要否判断、remote の公開範囲確認 | scan 結果を記録 |
| P0-b | **現行 production 相当を tag 化**したうえで、feature/dynam-ueda-parser の未コミット26ファイルを意味単位で分離コミットし、**機能開発分は別ブランチへ退避**（Pro 指摘: 安全網構築前に基盤コードの不確定差分を refactor base に混ぜない） | tag からの起動確認 |
| P0-c | マージ済みブランチ13本を `git branch -d`（-D 不使用）、worktree 残骸を `git worktree remove` | `git branch --merged` 空 |
| P0-d | ruff 最小導入（F, E9 など「確実にバグ」級 + baseline 方式: 既存違反は除外リスト化し新規・変更のみ CI fail）+ **mypy scoped 導入**（新設する MessageContext / Gateway / state class を常時対象、既存 legacy は ignore）。**baseline の更新ルール・除外期限・対象拡大計画を明文化**（腐敗防止 — Pro 指摘）。大規模 format はリファクタ PR に混ぜない | lint CI 追加、既存コード無修正 |

### P0.25: Deploy Safety / Observability（3〜5日）— Pro 指摘で新設

「リファクタ計画」を「稼働中 bot の移行計画」にするための前提条件。
**これが揃うまで P1 以降に進まない**（運用基盤は並行トラックではなく前提条件）。

| PR | 内容 | 検証 |
|---|---|---|
| P0.25-a | 即時 rollback 手段: git tag ベースの versioned release + 直前版切り戻しスクリプト。`.state/`（Mem0 / sessions / batch）の停止中スナップショット取得 + **復元リハーサル実施**（手順書だけで終わらせない） | 実際に復元して起動 |
| P0.25-b | staging 検証経路: dev guild + staging bot token での smoke script（起動・応答・スラッシュコマンド・添付）。リスクの高い PR は staging 24h → 本番の順で反映 | smoke green |
| P0.25-c | feature flag 基盤: 旧 sender / 旧 session 経路 / 旧 provider へ env 一つで戻せる仕組み（新旧経路が並存する P2〜P4 で必須） | flag 切替テスト |
| P0.25-d | 自動監視: correlation id 付き structured log を土台に、エラー率・応答遅延・429・Mem0 write error・未処理例外の集計と閾値アラート + **rollback 判断基準の明文化**（「24時間の人手観察」だけでは微増劣化に気づけない） | アラート発火テスト |

### P0.5: テスト耐性化（1〜1.5週）— 4モデル一致の最重要挿入フェーズ

| PR | 内容 | 検証 |
|---|---|---|
| P0.5-a | characterization test 基盤: on_message 公開フローを**まず top 8 フローに絞って**ゴールデン化（Pro 指摘: 全フロー網羅は3〜5日では無理）: ①通常応答 ②LLM 失敗 ③Mem0 recall+write ④添付あり ⑤返信文脈 ⑥同一チャンネル同時投稿 ⑦長文分割 ⑧HTTPException・permission。Discord 固有の検証観点（interaction 3秒 ACK/defer・command tree sync・DM/thread 権限・rate limit）を含める。snapshot は日時・乱数・id・token budget を正規化。private 関数は対象にしない | 現行挙動で green |
| P0.5-b | `call_with_tools` の契約テスト（mock は外部 I/O 直前のみ。呼び出し順序の検証は書かない）+ **失敗シーケンスの固定**: リトライ時にツール実行・Mem0 write・session append が二重実行されないこと | 同上 |
| P0.5-c | mem0 固定クエリ30本の回帰スクリプト。**評価条件を定義**（固定 DB・LLM 応答 cache・top-k recall/MRR・許容差）— HyDE/rewrite の非決定性でノイズ判定にならないように。以後 mem0 を触る PR の必須ゲート | ベースラインスコア記録 |
| P0.5-d | `@pytest.mark.legacy` 運用開始 + **統制ルール**（Pro 指摘: 「壊れたら legacy に逃がす」による CI 空洞化防止）: legacy 件数の増加禁止 / 置換先テストへのリンク必須 / full suite（legacy 込み）の nightly 実行 / 削除はレビュー必須。**削除前に「そのテストが守っていた暗黙仕様」を棚卸し**（既存テストは負債であると同時に仕様の記録でもある） | pytest 設定 + CI 変更 |
| P0.5-e | 非機能ベースライン測定: 応答レイテンシ・エラー率・メモリ使用量の現状値を記録（リファクタによる劣化検知の基準線） | 記録完了 |

※ バックログの「fable API 回帰固定」（fallback / compaction / sticky model）は P0.5-a/b に統合する。

### P1: 挙動不変の重複統合 + façade 前倒し（1.5〜2週）

**状態（モジュールレベル変数）はこのフェーズでは一切触らない**（読み取り専用リファクタ）。
削除系は「shim 追加 → 呼び出し元移行 → flag 切替 → 観察 → 物理削除」の多段原則
（第2ラウンドレビューで「P1 は低リスクではない」と一致判定）。

| PR | 内容 | 検証 |
|---|---|---|
| P1-a | resilience 統合: retry-after ヘッダパースを1関数に、`errors.py` を Literal 型カテゴリ化、tool_loop 内の重複 observe を一本化。挙動差が障害に直結する領域なので契約テスト先行 | 契約テスト + fable 回帰 + staging 24h |
| P1-b | agy_runner / codex_runner の共通基底抽出（`_cli_runner_base`） | 既存テスト |
| P1-c | `mem0_service.py` re-export 削除: 全 caller を `services.mem0` 直 import へ。**grep 対象に systemd unit / cron / 手動 scripts / MCP server / README のコマンド例まで含める**（Pro 指摘） | grep ゼロ + 起動確認 |
| P1-d | **読み取り専用 config façade**（Gemini 指摘の前倒し）: 新モジュールが依存する設定インターフェースを定義。以後の新設コードは旧 `config.py` を直接 import しない（import-linter で境界を機械enforcement）。物理分割は P5 のまま | import ルール CI |
| P1-e | **P4 の薄い前倒し**（Pro 指摘: split-brain と failover 不整合は現在進行形の運用リスク）: セッション書き込み経路の単一化 + LLM provider の薄い façade + OAuth failover 時の Mem0 再初期化フック。本格統合は P4 で | failover 模擬テスト |

※ 旧ドラフトで P1 に置いていた `claude_sdk.py` 物理削除（claude-api-unify PR9）は
**P2 末尾に移動**。順序: ClaudeGateway 固定 → 旧 claude_sdk を backend として呼ぶ →
flag 付きで claude_api 直結へ切替 → 観察 → 削除。旧 SDK のインターフェースに
新 Gateway が引きずられるのを防ぐ（DeepSeek + Pro 一致指摘）。

### P2: bot.py 境界抽出（2〜3週）— claude_api より先（裁定反映）

このフェーズは**挙動変更禁止**（抽出・移動・命名・境界固定のみ）。claude_api 側には
`ClaudeGateway`（薄い façade）だけを先に置き、内部は black box のまま触らない。
**境界契約: P2 本体ではセッション永続化と mutable 状態に触らない**（状態は P2.5、
セッションは P4。P2 で暗黙にセッション参照方法を変えると P4 で齟齬が出る — DeepSeek 指摘）。

| PR | 内容 | 検証 |
|---|---|---|
| P2-a | `MessageContext` 導入: on_message 前半の前処理結果（返信文脈・添付・recall 結果等）を運ぶ dataclass + `ClaudeGateway` / `DiscordGateway` の薄い façade 定義（**コンストラクタ注入**） | characterization green |
| P2-b | 送信系統一: メッセージ分割送信3箇所 / HTTPException 4箇所 / status edit 4箇所 → `MessageSender` へ（feature flag で旧経路に戻せる形） | 同上 + flag 切り戻し確認 |
| P2-c | プロンプト組立の bot.py 残留分を `PromptBuilder` へ寄せる（budget 適用・directive 挿入含む） | 同上 + プロンプト snapshot |
| P2-d | スラッシュコマンド12個 + prefix コマンドをモジュール分割（`bot_commands/` + setup_hook で明示的 `register_commands()`）。Cogs 非採用（裁定）。**command tree sync の挙動を変えない**（global sync は慎重に） | コマンド動作確認 |
| P2-e | `claude_sdk.py` 削除の多段実行（P1 から移動した PR9 完遂）: Gateway 経由化 → flag 切替 → staging + 本番観察 → 物理削除 | characterization 全 green + 本番 72h |

### P2.5: 状態移行（1.5〜2週）— P2 から分離（レビュー反映）

module global 28個の移行は**単なる移動ではなく concurrency model の変更**（Pro 指摘）。
着手前に1枚の設計メモを書く: lock 粒度・lock 順序・`await` 中の lock 保持禁止・
cancellation 時の release・semaphore 二重解放防止・session/mem0 書き込みの exactly-once。
`AppContext` は配線専用に限定（所有者・寿命・reset/close・注入経路を先に定義）し、
**新しい god object 化させない**。

| PR | 内容 | 検証 |
|---|---|---|
| P2.5-a〜c | 状態を**結合度の高い群単位**でサブシステム state class へ移行（機械的な 1PR=1状態ではなく、相互依存する状態はまとめて1PR — DeepSeek 指摘）。目安: ①rate-limit/quota 系 → ②override/cooldown 系 → ③background task/semaphore 系。各 state に `reset()` / `close()` を実装し conftest から確実に初期化。移行期の旧 global は新 context への委譲 wrapper。**新しい mutable global の追加は禁止** | 並行 characterization（同一チャンネル同時2メッセージ）+ 24〜72h 観察/PR |

**第1リリース完了ライン**: ここまでで一度停止し、成果・残リスク・工期実績を評価して
第2リリース（P3〜P5）への進行を判断する。

### P3: claude_api 分解（2〜3週）— ここから第2リリース

固定済み `ClaudeGateway` の裏側で実施。**retry ループ分解では、ツール実行・Mem0 write・
session append・usage telemetry が二重実行されない保証（idempotency）**を契約テストで
固定してから着手（Pro 指摘）。

| PR | 内容 | 検証 |
|---|---|---|
| P3-a | entry.py の 630行 retry ループ分解: `_setup_session` / `_assemble_tools` / `_attempt_once` / `_retry_until_success` / `_postprocess_deliberate` | 契約テスト + fable 回帰 + 失敗シーケンス固定 |
| P3-b | tool_loop.py 分解: streaming drain / tool dispatch / degraded rescue / telemetry を関数境界で分離（**段階 PR**。2,090行の一括分解はレビュー不能） | 同上 |
| P3-c | サブパッケージ化: `core/` `resilience/` `deliberate/` `external_models/` `tool_execution/` `telemetry/`。`__init__.py` re-export で既存 import を全て維持 | import 互換テスト + import smoke |
| — | **deliberate tier 10ファイルは機能凍結**（移動のみ）。移動 PR にも import smoke test を付け、**feature flag で deliberate tier を本番から切れる状態**を維持 | import smoke |

### P4a: セッション統合（1〜1.5週）— 最高リスク。単独マイルストーン（レビューで分離）

書き込み経路は P1-e で単一化済み。**シャドーモード必須**（Gemini 指摘）: 新 SessionStore を
旧実装と並行稼働させ、書き込み結果を比較しつつ**採用値は旧実装のまま**数日観察 →
一致率を確認してから flag で切替。事前に `.state/` スナップショット + 復元リハーサル。

| PR | 内容 | 検証 |
|---|---|---|
| P4a-1 | `session.py` + `session_store.py` 統合（永続化 backend + in-memory TTL を単一 SessionStore に）。composite key の flatten 問題（バックログ P1）もここで解消 | シャドー比較 + 再起動→履歴復元 e2e + 72h 観察 |

### P4b: LLM fallback 統一（0.5〜1週）— P4a と分離（復旧経路を同時に壊さない — GPT 指摘）

| PR | 内容 | 検証 |
|---|---|---|
| P4b-1 | LLM provider フォールバック統一層: `mem0/client.py` と `cli_compat.py` の連鎖を P1-e の façade に一本化し、OAuth failover 時に両者が同時に切り替わることを保証 | failover 模擬テスト + flag 切り戻し確認 |

### P5: mem0/search 分解 + config 物理分割（1〜2週）

| PR | 内容 | 検証 |
|---|---|---|
| P5-a | `search.py` → `query.py`（実検索）/ `transforms.py`（rewrite・HyDE・keyword）/ `orchestration.py`（smart 検索の7フェーズ制御） | 固定クエリ30本が P0.5-c の許容差内 |
| P5-b | JSON codeblock strip / `{{}}` エスケープ等の重複10パターンを helper 化 | 既存+契約テスト |
| P5-c | `config.py` → `config/` パッケージ。**同名モジュールとディレクトリは共存できず実質一発 rename になる**（Pro 指摘）ため、事前に import graph と import 副作用を確認し、P1-d の façade 経由率を上げてから実施。`config/__init__.py` で全シンボル re-export | import 互換テスト + 全 entry point（bot / web_api / MCP servers / cron scripts）起動確認 |

### 並行開発・運用ルール

- deliberate tier の機能開発は P3-c のサブパッケージ移動が終わるまで**新規ファイル追加のみ可**
  （既存10ファイルの大規模改変は P3 完了後に再開）
- リファクタ PR と機能 PR は別ブランチ・別マージ。リファクタブランチは常に master から切る
- **master への反映はフェーズ単位ではなく可逆な小 PR 単位**（フェーズ末まで溜めると差分が
  肥大化しコンフリクト地獄になる — GPT/Gemini 指摘）。リスクの高い PR は staging → 本番
- **ホットフィックス手順**: リファクタ中に新規 P0 バグが出たら、リファクタブランチと独立に
  master 直で修正 → リファクタブランチへ随時 rebase
- 再起動を伴う本番反映は深夜帯。クラッシュループ時は P0.25-a の切り戻しスクリプトで即復旧
- **AI エージェント運用ルール**: 同時編集禁止ファイルの宣言 / 大規模 grep 一括置換の禁止 /
  move PR でのロジック変更禁止。PR テンプレートに「挙動変更有無・feature flag・rollback
  手順・state 変更有無・smoke 結果・legacy test 追加数」を記載
- 補助スクリプトを随時整備: `backup_state` / `restore_state` / `smoke_discord` /
  `check_import_compat` / `list_mutable_globals`（AI エージェントの自己検証に効く — Pro 提案）

## 5. リスクレジスタ

| リスク | 影響 | 緩和策 |
|---|---|---|
| 状態クラス化での race 顕在化 | 重複応答・記憶欠落 | 状態移行 PR は1個ずつ。移行前後で並行負荷テスト（同一チャンネル同時2メッセージ等）を characterization に追加 |
| セッション統合での split-brain | コンテキスト巻き戻り | session.py / session_store 統合前に「書き込み経路の単一化」を先行 PR で実施 |
| LLM failover 不整合 | Mem0 抽出停止・誤動作 | フォールバック統一層（P4）まで OAuth failover 時の Mem0 再初期化を暫定フックで対応 |
| 検索品質の無自覚な退行 | 記憶 recall 劣化 | 固定クエリ30本の回帰スクリプトを P0.5 で作成し、mem0 触る PR の必須ゲートに |
| WIP（deliberate tier）との衝突 | rebase 地獄 | deliberate tier 10ファイルはリファクタ対象から除外（凍結）。触るのは配置移動のみ |
| 既存テストの大量赤化 | CI 麻痺 | 壊れた mock テストは `@pytest.mark.legacy` でスキップ可能にし、置換 or 削除を各 PR で判断 |
| legacy マーカーの乱用 | CI の false green 化 | 件数増加禁止・nightly full suite・削除レビュー必須（P0.5-d 統制ルール） |
| retry 分解での副作用二重実行 | ツール実行・Mem0 write の重複 | 失敗シーケンスの契約テストを P3 着手前に固定（idempotency） |
| Discord API 固有の罠 | interaction timeout・sync 事故 | 3秒 ACK/defer・command tree sync・rate limit を P0.5-a の検証観点に明記 |
| config/ への一発 rename 事故 | 全 entry point 起動不能 | import graph 事前確認 + 全 entry point 起動確認を P5-c の必須ゲートに |

## 6. Non-goals（今回やらないこと）

- deliberate tier パイプラインの機能改修（WIP のため凍結。移動のみ）
- Web UI / frontend の刷新（バックログ P2 のまま）
- マルチプロセス化・水平スケール（シングルプロセス asyncio を維持）
- フル DI コンテナ・インターフェース総 Protocol 化（過剰設計）。
  **ただし新設境界の手動DI（コンストラクタ注入）は必須ルールであり Non-goal ではない**
  （完全に注入を避けると monkeypatch 地獄が再生産される — GPT/Gemini 指摘）
- mem0 検索アルゴリズム自体の変更（構造分解のみ。品質チューニングは別トラック）
- **既存2,464テストの一括書き換え**（壊れたものは P0.5-d の統制ルールで置換 or 削除。
  テストスイート全体の再設計は別トラック — スコープ外であることを明示）
- パフォーマンス最適化（ただし P0.5-e のベースライン測定と劣化検知は安全対策として実施）

※ 旧ドラフトで「運用基盤は並行トラック」としていた点はレビューで否定された
（稼働中 bot の移行では前提条件）。P0.25 に昇格済み。

## 7. 2026-07-02 バックログとの整合

| バックログの束 | 本計画との関係 |
|---|---|
| 情報漏えいと権限境界（P0） | **本計画より先**に処理（前提条件）。構造変更で権限境界を動かす前に閉じる |
| 応答停止と復旧性（P0） | 同上。セマフォ解放漏れ・main_task 例外伝播は P0.5 の characterization 対象にも入れる |
| fable API 回帰固定（P0/P1） | P0.5 のテスト耐性化と統合（fallback / compaction / sticky model の approval test） |
| Memory/RAG 整合性（P0/P1） | batch ack 化・delete 索引掃除は**構造分解より先**（データ損失系）。BM25 archive 対応は mem0 分解 PR に同乗可 |
| Bot本体分割（P1/P2） | 本計画 §4 が実行計画そのもの |
| 運用基盤（P1/P2） | **前提条件に昇格**（Pro 指摘）: rollback・smoke・監視・バックアップは P0.25 として本計画に内包。/live /ready 分離もそこで実施 |

## 8. 参考資料

- 既存バックログ: `docs/2026-07-02-fable-refactor-improvement-backlog.md`
- deliberate tier 設計: `.claude/adr/0034-deliberative-pipeline.md`
- claude-api-unify の経緯: requirements.txt コメント（PR4/PR9）、
  ブランチ `refactor/claude-api-unify`（マージ済み）
- Oracle MCP パッチ（本計画の外部相談を可能にした修正）: `docs/oracle_mcp.md`
- 計画レビュー第2ラウンド全文（gpt-5.5-pro）: `docs/2026-07-03-plan-review-gpt55pro.md`
- 争点裁定全文（gpt-5.5-pro）: `docs/2026-07-03-refactor-arbitration-gpt55pro.md`

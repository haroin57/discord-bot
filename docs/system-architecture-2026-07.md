# かなえ Discord Bot — システムアーキテクチャ（2026-07 現行版）

> 作成: 2026-07-04 / コードベース全走査に基づく現行版。
> 旧 `docs/architecture.md`（〜2026-06）を土台に、今日時点の実装で全面改訂。
> **旧版からの主要変更は §12 に集約**（感情エンジン・Web UI・SDK backend の廃止、Cloudflare オフロード、熟考パイプライン等）。
> 機密（トークン・webhook URL・KV id・account id）は本文に一切含めない。

---

## 1. 概要

Claude（Anthropic）をバックエンドとする自律エージェント型 Discord Bot。単なる API ラッパーではなく、長期記憶・エージェントループ（ツール実行）・熟考/合議パイプライン・セッション持続・動的ツール選択を持つ。コアは **Ubuntu VPS 1台（8GB RAM + 8GB Swap / 4 vCPU）** の Python プロセスで、周辺の定期実行・監視・重処理を **Cloudflare Workers / Browser Rendering** にオフロードして本体を軽量化している。

**技術スタック（現行）**

| 層 | 技術 |
|---|---|
| ランタイム | Python 3.12 / asyncio（単一プロセス, `main_async.py`） |
| Discord | discord.py (`commands.Bot`) |
| AI メイン | Claude `opus-4-8`（OAuth subscriber 認証, `CLAUDE_MODEL`） |
| AI 抽出 | Claude Sonnet（Mem0 fact 抽出, `services/mem0/extract.py`） |
| AI 補助 | Claude Haiku 4.5（query rewrite / rerank / conflict 分類） |
| 外部相談 | GPT / DeepSeek（`second_opinion.py`） |
| Embedding | OpenAI text-embedding-3-small（Mem0 / semantic selector 共用） |
| ベクトルDB | ChromaDB（server mode, `127.0.0.1:8765`） |
| Entity Graph | SQLite（`services/mem0/graph.py`） |
| 投資KB | LanceDB（`services/invest_kb/`） |
| MCP | context7 / brave-search(remote) / x-scraper / playwright / mem0 / 金融データ群 ほか |
| レンダリング | Cloudflare Browser Rendering（table/math → PNG） |
| インフラ | systemd（bot 本体 + ChromaDB + MCP 共有ゲートウェイ） + Cloudflare |

---

## 2. アーキテクチャ全体像

```
┌──────────────────────────────────────────────────────────────────┐
│ ユーザー (Discord)                                                 │
└───────────────┬──────────────────────────────────────────────────┘
                │ Discord Gateway (discord.py)
════════════════▼══════════════════════════════════════════════════
   かなえ本体 (Python / asyncio, main_async.py)
                │
   ┌────────────▼───────────────────────────────────────────────┐
   │ ① 受信・入場制御   bot.py::on_message                       │
   │   Bot除外 / DM制限 / メンション検知 / prefixコマンド        │
   │   admin判定(ADMIN_USER_IDS) / 非adminクールダウン・日次quota│
   │   チャンネルセマフォ(1req/ch, FIFO)                         │
   ├────────────▼───────────────────────────────────────────────┤
   │ ② 並列プリプロセス bot_preprocess.py                        │
   │   リプライ先/添付 · 履歴 · リンク解決 · クロスsレッド ·     │
   │   Mem0検索 · invest_kb検索 · 画像DL · 履歴keyword一致        │
   ├────────────▼───────────────────────────────────────────────┤
   │ ③ プロンプト組立 bot.py + services/prompt_builder.py        │
   │   信頼境界タグ(trusted/untrusted) · retrieved_memories ·    │
   │   history_search · 話者ヘッダ · 就寝ディレクティブ(ADR-32)  │
   │   sentinel.py で正規化+タグ偽装除去                          │
   ├────────────▼───────────────────────────────────────────────┤
   │ ④ エージェントループ services/claude_api/                   │
   │   entry.call_with_tools → tool_loop.run_agent_loop          │
   │   ├ 三層ツール選択(ADR-0033) + MCP lazy-spawn               │
   │   ├ streaming + tool_use実行 + in-loop compaction(ADR-12)   │
   │   ├ 熟考: chat/deliberate tier · planner · verifier         │
   │   ├ 合議: MAGI-lite(route_judgment) · second_opinion        │
   │   └ rate-limit pacing + server-side fallback(ADR-15)        │
   ├────────────▼───────────────────────────────────────────────┤
   │ ⑤ 出力 bot_ui.py + utils/*                                  │
   │   sanitize → table/math を CF Browser Rendering で PNG化 →  │
   │   2000字分割 → 画像/ファイル添付 → reply                    │
   ├────────────▼───────────────────────────────────────────────┤
   │ ⑥ 背景処理(fire-and-forget)                                 │
   │   bot_record.py: Mem0 ターン記録 / JSONL履歴append          │
   └────────────────────────────────────────────────────────────┘
                │                              ▲
     health_server(:3002 /health)             │ cloudflared tunnel
                │ health.haroin57.com          │
════════════════▼══════════════════════════════╪═══════════════════
   Cloudflare（周辺オフロード §9）             │
   health-probe(毎分) · intervention(30分) · vi-signal(平日) ·     │
   notify(通知ハブ) · mcp-brave(検索MCP) · egress-poc · Browser    │
   Rendering · kanae-web(Next.js) · kanae-scheduler(日次)          │
════════════════════════════════════════════════════════════════════
```

---

## 3. プロセス構成

エントリは `main_async.py`（旧 `main.py` は Discord 単独起動の遺構）。

```
main_async.py  (pidfile flock で二重起動防止, main_async.py::_acquire_pidlock)
  └ asyncio.gather:
       ├ bot.start(DISCORD_TOKEN)                     … Discord Gateway
       └ health_server.start(:3002, 127.0.0.1)        … GET /health のみ
  ├ runtime_state.periodic_flush                       … 状態の定期永続化
  └ graceful shutdown (SIGTERM/SIGINT):
       kill_orphan_claude_cli / kill_orphan_mcp_processes → bot.close
```

- `services/health_server.py`：aiohttp で `127.0.0.1:3002` に `GET /health` だけを出す最小サーバー。旧 Socket.IO Web UI（`web_api.py`）が担っていた `/health` の生き残り。
- `services/health.py::get_health_snapshot`：`discord` / `oauth` / `mem0` / `system`(disk_pct, mem_pct, swap_pct, cpu_load_pct) を集約。Cloudflare health-probe がこの JSON を毎分読む（§9.1）。
- systemd ユニット（`infra/systemd/`）：`discord-bot-autodeploy.{service,timer}`（git pull → 再起動の自動デプロイ）, `chromadb-server.service`, `mcp-{mem0,oracle,x-scraper,second-opinion}-shared.service`, dev 環境用の `discord-bot-dev-autodeploy.*`。

---

## 4. Discord Bot — 受信からプロンプト組立まで

### 4.1 受信・入場制御（`bot.py::on_message`, 659行〜）

1. Bot 自身/他 Bot 除外、DM は admin のみ許可
2. 非メンション時はチャンネルインデックス（`_index_channel_message`）だけ回して return
3. `bot_commands.prefix.handle_prefix_commands`：`/research` 等の prefix/slash コマンド分岐
4. 非 admin はクールダウン（`services/rate_limit.py`）+ 日次 quota
5. チャンネルセマフォ（`_get_channel_sema`, LRU 500）で 1 チャンネル 1 リクエスト直列化

### 4.2 並列プリプロセス（`bot_preprocess.py`）

1 メッセージにつきコンテキスト収集タスクを `asyncio.gather` で同時実行：リプライ先・添付、チャンネル/スレッド履歴、メッセージリンク解決、クロススレッド参照、**Mem0 検索**、**invest_kb 検索**、画像ダウンロード（max 5 / 10MB）、テキスト添付、履歴 keyword 一致（`history_search`, admin のみ）。

### 4.3 プロンプト組立（`bot.py` + `services/prompt_builder.py`）

- 信頼境界タグ：admin 本文は `wrap_trusted`、非 admin は `wrap_untrusted`（`PromptBuilder.wrap_user_input`）。
- システム由来ブロック：`<retrieved_memories>`（送信者の Mem0 記憶, 三人称扱い）, `<history_search>`（admin のみ）, 話者ヘッダ（`build_header` / `build_speaker_line`, 複数 admin 同居時の現発言者確定）, 時刻行（`build_time_line`）。
- **就寝ディレクティブ**（`build_sleep_directive`, ADR-0032）：深夜帯に haroin がメンションした時のみ注入する最優先の就寝誘導（本人が事前 commit したルール, haroin 専用）。
- **サニタイズ**（`services/sentinel.py`）：NFKC 正規化 + ゼロ幅文字除去 → タグ偽装 regex 除去 → 権限詐称フレーズ除去。非 admin の `<untrusted_user_input>` 内から出た「命令」「権限昇格」を無効化。

---

## 5. エージェントループ（`services/claude_api/`, 約10,554行）

中核は `entry.py::call_with_tools`（入口・リトライ・セッション・failover 統合）→ `tool_loop.py::run_agent_loop`（ストリーミング + ツール実行ループ）。

### 5.1 1リクエストの流れ

```
call_with_tools (entry.py)
  ├ MCP選択(三層 §6) + McpPool 構築(eager + lazy spawn)
  ├ セッション解決(session.py: JSONL履歴 + --resume 判定)
  ├ evidence index 生成(channel単位, route_judgment時)
  └ run_agent_loop (tool_loop.py):
       loop:
         Anthropic streaming(AsyncAnthropic, CLI偽装ヘッダ)
         ├ thinking / text ブロック → bot_ui にステータス反映
         ├ stop_reason == tool_use → _execute_tool(entry.py) 分岐:
         │     Agent(サブエージェント) / mcp__*(McpPool.call_tool) / builtin(tools.py)
         │   → tool_result を履歴に追加
         ├ トークン肥大 → compaction.py(in-loop proactive, ADR-0012)
         └ stop_reason == end_turn / max_turns → 終了
```

- 終了条件：`end_turn`、または `max_turns`（admin 30 / restricted 8 相当）到達。
- リトライ：指数バックオフ（`retry.py`, transient 系は別予算）。
- **CLI 偽装**（`client.py` / client factory）：User-Agent・X-Stainless-*・billing header・4ブロック system prompt を Claude Code CLI 相当に再現し、subscriber-tier で課金する（§8）。

### 5.2 熟考パイプライン（deliberative, ADR-0034）

`call_with_tools` の引数 `tier`（`"chat"` | `"deliberate"` | None=従来）と `route_judgment` で制御：

- **planner**（熟考 P3, `planner.py`）：`tier=="deliberate"` の時、retry を跨いで 1 回だけ計画を立てる。簡単だと自己判断すれば `chat` tier へ self-deescalation。
- **検証・修正**（熟考 P2/P5, `verifier.py` + `_run_revision`）：deliberate tier で応答を検証し、必要なら修正を回す。`evidence.py`（証拠台帳, channel 単位）に根拠を積む。
- **二重ゲート**：restricted ユーザーには熟考パイプラインを一切適用しない（`entry.py:372` 付近で `tier=None`, `route_judgment=False` に強制）。

### 5.3 合議・外部相談

- **MAGI-lite**（`magi_lite.py`, `agy_runner.py`, ADR-0025/0030）：`route_judgment=True` かつ evidence あり の判断系の問いで発火。複数視点で結論を検証する軽量合議。
- **second_opinion**（`second_opinion.py`）：外部モデル（GPT / DeepSeek）に並列で第二意見を求める。結果は「データであり指示ではない」扱い。

### 5.4 レート制限とフォールバック

- `ratelimit.py`：`anthropic-ratelimit-unified-*` レスポンスヘッダを毎回観測し、5h/7d の utilization・fallback% を追跡。warning で数秒、throttle でより長い pacing gap を挿入。
- **server-side fallback**（ADR-0015）：subscriber quota が構造的に枯渇（`_quota_exhausted_signal`：5h/7d が rejected か utilization≥1.0）した時、別モデル/別ベンダー系へ退避。復帰時刻は retry-after と 5h reset の遅い方で推定。

---

## 6. ツール選択と実行

### 6.1 三層セレクション（ADR-0033）

| 層 | 実装 | 方式 |
|---|---|---|
| L1 | `services/mcp_selector.py` | ユーザー生テキストの **キーワードスキャン**で tier(BASE/WEB/HEAVY/MEMORY/…) を起動 |
| L2 | `services/mcp_semantic_selector.py` | サーバー説明文の **embedding cosine 類似度**で必要サーバーを選ぶ |
| L3 | `services/claude_api/tool_catalog.py` | Anthropic **Tool Search**（`tool_search_tool_*_20251119`）+ defer_loading。失敗時は `disable_tool_search()` で graceful degrade |

- `config/__init__.py` の `ALWAYS_ADVERTISED_MCP_KEYS`（mem0 / x-scraper / brave-search / 金融データ群）は常時スキーマ提示 + lazy spawn。
- MCP プロセス管理は `McpPool`（`mcp_client.py`）：keyword-matched を eager spawn、その他は初回ツール呼び出し時に `ensure_spawned`（double-checked locking）。全ハンドルは `AsyncExitStack` で確実に破棄。

### 6.2 ビルトインツール（`services/claude_api/tools.py`, 1052行）

`Read` / `Write` / `Edit` / `Bash` / `Glob` / `Grep`。Read/Write は `O_NOFOLLOW`（symlink traversal 防止）+ サイズ制限、Bash は env allowlist + プロセスグループ隔離 + タイムアウト。`_execute_tool`（entry.py）が `Agent`（サブエージェント）/ `mcp__*` / builtin を振り分ける。

### 6.3 サブエージェント / deep_research

- **Agent ツール**（`agent_tool.py`）：親トランスクリプトを共有しない fresh context の汎用サブエージェント。
- **deep_research**（`services/deep_research/`, ADR-0028）：`/research` から fire-and-forget、専用スレッドで多段 Web 調査。Phase1 は planner → 並列 searcher → synthesizer の multi-agent。**session/JSONL を使わず**中間状態は `ResearchState` のみ、サブエージェントは `run_agent_loop` 直呼びで claude_semaphore を通らない。MCP は 1 プール共有 + server-key lock。

---

## 7. 長期記憶

### 7.1 Mem0（`services/mem0/`, 約6,058行）

ChromaDB は **server mode**（`127.0.0.1:8765`）で常駐し、bot 本体と mem0 MCP の双方が HTTP クライアント経由でのみアクセス（2026-05 の HNSW 破損インシデント対策。単一書き込み窓口に集約）。

- **Ingest**（`ingest.py`）：会話 → `extract.py`(Sonnet fact 抽出) → importance ゲート → intra-batch dedup → `dedup.py`(2段: ベクトル距離 → Haiku 意味判定) → `conflict.py`(update/correction/addition/none 分類, 旧 fact に `valid_to` 打刻でソフトアーカイブ) → `reinforce.py`(Hebbian: mention_count++) → `graph.py`(entity upsert + fact↔entity edge) → ChromaDB raw insert。
- **Search**（`search.py`）：alias 展開(graph) → Haiku query rewrite → multi-query ベクトル検索 → 1-hop graph 展開 → reinforcement boost → Haiku rerank(top-5)。`_is_fact_valid` が `valid_to<=now` を常時フィルタ（env でロールバック可）。
- 分類 metadata：8 カテゴリ taxonomy + importance(1-5) + tier(0-3) + dedup_group。ingest 時に Haiku 4.5 が自動付与。
- 週次メンテ：日曜 9 時 cron（`scripts/weekly_mem0_compact.py`）で tier 減衰・重複整理・話者混入回帰チェック。
- **namespace 分離**：Discord user snowflake ごと。初回ターンのみ `<retrieved_memories>` として送信者本人の記憶を注入。
- ※記憶再設計の検討は `docs/memory_redesign_plan.md`（別途）。既存層の穴 (attribute/condition/scope) を塞ぐ方針。

### 7.2 invest_kb（`services/invest_kb/`, ADR-0019, LanceDB）

Mem0 とは別の side store。EDINET narrative・ブログ・CFA ノート等の**長文の中立的投資知識**を、raw chunk + Haiku 生成の要約/キーワード/entities として LanceDB に保持。Mem0 の「fact 抽出 + 120字圧縮」を通すと情報が落ちる/dedup に引っかかる長文を、fact 距離空間を汚染せずに並列 recall する。

### 7.3 セッション持続（`services/session.py`）

Anthropic API はステートレスなので、チャンネル別 **JSONL append-only transcript** を毎ターン保存し次ターンで replay。`SessionInfo`（channel_id → `--resume` メタ）を LRU（`MAX_SESSIONS`）管理。`clear_channel_context` は JSONL を `.cleared-<ts>` にリネームしてセッションを破棄（次ターンは cold-start）。30 分無活動でセッションはリセット。

---

## 8. 認証・課金（`services/oauth_manager.py`, 973行）

- Claude Code OAuth（`~/.claude/.credentials.json`）を読み、有効期限 5 分前に自動リフレッシュ。Anthropic SDK に monkey-patch / AsyncAnthropic に直接注入。
- **subscriber-tier 識別**：Python SDK デフォルトヘッダだと一般 OAuth バケット扱いになり subscriber quota を使えない。client factory が Node.js CLI の User-Agent・X-Stainless-*・billing header・4ブロック system prompt を完全再現し、subscriber tier で呼ぶ。
- OAuth 失効時は抽出/rerank 系 LLM を DeepSeek V4 Flash 等へフォールバック（`services/mem0/client.py::get_anthropic_client`）。

---

## 9. Cloudflare 実装

ソースは `/home/ubuntu/archives/cloudflare-workers-src-20260704/`（デプロイ済みソースの退避）+ `/home/ubuntu/kanae-web`, `/home/ubuntu/kanae-contact`。本体 VPS との連携は「本体は `/health` を出すだけ、監視・定期・重処理は Cloudflare 側から」という**責任分離**。

### 9.1 kanae-health-probe（毎分 cron）
`src/index.js`。`https://health.haroin57.com/health`（cloudflared tunnel → 本体 :3002）を毎分 fetch。3 連続失敗で DOWN / 2 連続成功で UP。加えて `subsystems.system` の生値に閾値（disk 85% / mem 90% / swap 50% / cpu_load 150%）+ ヒステリシスで WARN/CLEAR。状態は `MONITOR_KV`、通知は Discord webhook（username「かなえ外形監視」）。**監視が監視対象と別プロセス**なので本体ごと落ちても通知が出る。

### 9.2 kanae-intervention（30分 cron, ADR-0027）
`src/index.js`。USD/JPY 為替介入アラート。Yahoo Finance 4h チャートから 6 シグナル（水準/変動ペース/口先/レートチェック/時間帯/Polymarket 165到達確率）をスコアリング、合計 8 点超で **notify worker に POST**。4 時間クールダウン、状態は `INTERVENTION_KV`。移植元は `scripts/intervention_alert.py` / `services/intervention_scheduler.py`。

### 9.3 kanae-vi-signal（平日 09:00 UTC = 18:00 JST cron, ADR-0011/0026）
`src/index.js`。日経 VI 平均回帰戦略。Yahoo Finance の `^VIX` / `^N225` / `HYG` から `c1: VIX始値≥30 && c2: VIX終値>始値 && c3: N225終値<始値`（HYG 60日 drawdown をシャドーフィルタ）を判定、fire で notify worker に POST。状態は `CRON_STATE` KV。移植元は `scripts/vi_signal_alert.py`。

### 9.4 kanae-notify（通知ハブ）
`src/index.ts`。intervention / vi-signal / health からの POST を受け、`WEBHOOK_HEALTH` / `WEBHOOK_VI_SIGNAL` / `WEBHOOK_INTERVENTION` に振り分けて Discord へ送る共通ハブ。認証は `X-Notify-Auth`（Bearer）、2000 字分割・リトライ内蔵。通知先の webhook を 1 箇所に集約。

### 9.5 kanae-mcp-brave（Brave 検索 MCP）
`src/index.ts`。MCP Streamable HTTP サーバー（`initialize` / `tools/list` / `tools/call`）を Worker で実装。認証は `X-MCP-Auth`。**bot 側は `config/__init__.py` の `BRAVE_MCP_REMOTE_URL` + `BRAVE_MCP_AUTH_TOKEN` があれば `type:"http"` でここに接続、無ければローカル npx stdio にフォールバック**（2026-07-04 切替、initialize 200 OK 確認済み）。BRAVE_API_KEY を Worker secret に隔離し、ローカル node プロセスを 1 本削減。

### 9.6 kanae-egress-poc / kanae-scheduler / kanae-web
- **egress-poc**：Yahoo Finance（VIX/USDJPY）を Cloudflare 経由で fetch する PoC。VPS からの egress が不安定/ブロックされた場合の迂回検証。
- **kanae-scheduler**：`deploy/kanae-scheduler.service`（systemd, VPS 常駐）で動く日次配信スケジューラ。リポジトリには webhook/direct/vps-test 等の実験版 `.mjs` が併存。
- **kanae-web**：Next.js（`app/` + `ecosystem.config.cjs` = PM2 常駐）。haroin57.com 系の Web フロント。**kanae-contact** は連絡系 TypeScript サービス。

### 9.7 Cloudflare Browser Rendering（table/math レンダリング）
`utils/cf_browser.py::render_screenshot`（aiohttp, 30s タイムアウト, 429/5xx のみ 1 リトライ, 失敗時 None→text フォールバック）。`utils/table_renderer.py` / `utils/math_renderer.py` がローカル Puppeteer デーモンからこの REST スクリーンショット API に差し替え済み（呼び出し側 IF 不変）。CF がブラウザを温存するため cold start が消え、実測 cache-miss 0.35〜1.61s（旧ローカル cold 9.1s から改善）。

---

## 10. データフロー（1リクエスト要約）

```
Discord → on_message
  ├ gate(bot/DM/mention/prefix/cooldown/quota) → channel semaphore
  ├ [並列] bot_preprocess: 履歴 / Mem0 / invest_kb / 添付 / リンク / history_search
  ├ prompt組立(bot.py + prompt_builder + sentinel)
  ├ call_with_tools(entry) → run_agent_loop(tool_loop):
  │     三層ツール選択 → streaming → tool_use(builtin/mcp/agent) → compaction
  │     (deliberate: planner→verifier / judgment: MAGI-lite / quota枯渇: fallback)
  ├ 出力(bot_ui): sanitize → table/math=CF Browser Rendering → 2000字分割 → 添付 → reply
  └ [背景] bot_record: Mem0 ターン記録 + JSONL append
外部(常時): CF health-probe→/health / CF intervention・vi-signal→notify→Discord
```

---

## 11. セキュリティモデル

- **二重権限**：admin（`ADMIN_USER_IDS`）は全ツール + MCP + Mem0 読み書き。restricted は**ツール/MCP 一切なし**・CWD を tmpdir 隔離・専用 API キーで課金分離・Mem0 書き込み不可（poisoning 防止）・熟考パイプライン不適用。
- **プロンプトインジェクション防御**（`sentinel.py`）：NFKC 正規化 + ゼロ幅除去 + タグ偽装 regex + 権限詐称フレーズ除去 + `<untrusted_user_input>` ラッピング。信頼境界タグは `prompt_builder` が正規に付与（本文だけ読む）。
- **ファイル**：state/history は mode 0600、`utils/atomic_write.py`（mkstemp→fchmod→rename）、Read/Write は O_NOFOLLOW、画像添付は許可ディレクトリのホワイトリスト。
- **プロセス衛生**：pidfile flock で二重起動防止、`kill_orphan_claude_cli` / `kill_orphan_mcp_processes`（psutil）で孤児回収。
- **Cloudflare 側**：Worker 間は `X-Notify-Auth` / `X-MCP-Auth`、秘密は wrangler secret に隔離、本体は `/health` を localhost + tunnel 経由のみ公開。

---

## 12. 旧 architecture.md からの主要変更（2026-06 → 2026-07）

**廃止された機能（旧版に記載があるが現行に無い）**
- **感情エンジン**（`services/emotion/` 一式・Haiku 常駐デーモン・6次元感情ベクトル・沈黙ティック・自発連絡）→ ディレクトリごと削除。bot.py に残る `emotion` はコメントのみ。
- **かなえ自己記憶**（kanae_self namespace）・`<kanae_state>` / `<kanae_daily>` プロンプトブロック → 廃止（2026-06-08）。
- **Socket.IO Web UI**（`web_api.py`）→ 廃止。`/health` だけ `services/health_server.py` に縮小移管。
- **SDK バックエンド**（`services/claude_sdk.py`, Node.js CLI subprocess）→ 削除。API 直接（`services/claude_api/`）に一本化。

**追加/変更された機能**
- **Cloudflare オフロード**：health-probe / intervention / vi-signal / notify / mcp-brave / egress-poc / Browser Rendering / scheduler（§9）。
- **熟考・合議パイプライン**：chat/deliberate tier・planner・verifier・evidence・MAGI-lite（§5.2–5.3, ADR-0025/0030/0034）。
- **deep_research**（§6.3, ADR-0028）、**invest_kb**（§7.2, ADR-0019）。
- **bot.py 分割**：`bot_preprocess.py`（並列プリプロセス）/ `bot_record.py`（Mem0 記録）/ `bot_ui.py`（ステータス UI, ADR-0023/0029）。
- **三層ツール選択**（§6.1, ADR-0033）。
- **スペック**：2GB RAM → 8GB + 8GB Swap / 4 vCPU。**モデル**：opus-4-6 → `opus-4-8`。
- **config.py → `config/` パッケージ**、**`main.py` → `main_async.py`**。

---

## 13. 関連 ADR / ドキュメント索引

| 領域 | 一次資料 |
|---|---|
| in-loop compaction | ADR-0012 |
| server-side fallback | ADR-0015 |
| インライン描画/spinner | ADR-0023 / 0029 |
| MCP direct stdio | ADR-0022 |
| skills/subagents/MAGI | ADR-0025 |
| VI 戦略 / fwd 記録 | ADR-0011 / 0026 |
| deep research | ADR-0028 / 0030 |
| 介入監視 | ADR-0027 / 0028(oanda) |
| 就寝強制 | ADR-0032 |
| 三層ツール選択 | ADR-0033 |
| 熟考パイプライン | ADR-0034 |
| 記憶再設計 | `docs/memory_redesign_plan.md` |
| ChromaDB server mode | `docs/chromadb-server-mode-requirements.md` |

---

## 14. 未確認・今後の照合ポイント

- `kanae-web` / `kanae-contact` のデプロイ先（CF Pages か VPS PM2 か）は `ecosystem.config.cjs` の存在から PM2 常駐と推定。確定は wrangler/PM2 の稼働状況で要照合。
- `archives/` 配下の Worker ソースは「現行デプロイの退避」と判断（日付 2026-07-04・cron/KV が本文の連携と整合）。廃止済みか稼働中かは `wrangler deployments list` で最終確認する余地あり。
- `kanae-scheduler` は複数の実験版 `.mjs` が併存。現行は systemd 版（`deploy/kanae-scheduler.service`）と判断。

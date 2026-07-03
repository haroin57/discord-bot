# 抜本オフロード計画 v2 — Opus 12体走査 + 4モデル合議の統合版（2026-07-04）

前版 `docs/2026-07-04-cloudflare-offload-plan.md`（C0〜C10）の後継。
C0b/C0c/C0d（外形監視+journal配信）・C1（R2）・C2（Pages）・C3（brave Workers 化、
bot 側切替のみ未）は完了済み。本書はその先の「抜本」を、コードベース全域走査
（Opus 4.8 ×12体、361 tool calls）と外部4モデル（GPT-5.5 / Gemini 3.1 Pro /
DeepSeek / GPT-5.5 Pro）の合議で再設計したもの。

## 結論（4モデル全会一致 + 走査の裏付け）

**「Cloudflare へ全面移住」ではなく「VPS を薄い芯に痩せさせる」が正解。**
今回の文脈での「抜本的」とは Cloudflare 製品を増やすことではなく、
**死んだ常駐・壊れた cron・重複 spawn・bot を止めるバックアップを根絶すること**。

根拠となる走査の主要発見:

1. **前提崩壊①**: 「mem0 は Vectorize を公式サポート」は **TS SDK 限定**。
   Python SDK (mem0ai 1.0.11) に vectorize provider は存在しない。さらに
   Vectorize は metadata index 最大10/index に対し、現行 mem0 メタデータは
   約40キー使用 → provider 差し替えで済む話ではない（custom VectorStoreBase
   自作 + フィルタ再設計が必要な hard 案件）
2. **前提崩壊②**: 「MCP → Workers で -750MB」は過大。MCP 関連 3.9GB/106
   プロセスの大半は **並行 Claude Code 開発セッションの stdio MCP 重複**で、
   bot 本番は ADR-0022 の on-demand+self-reap 済み（idle 時ほぼゼロ）
3. **運用債務**（オフロード以前の問題）:
   - `backup_mem0.sh`（root cron）が**毎日 03:00 に bot+ChromaDB を停止**して
     rsync → 毎日ダウンタイム窓 + .state 内に8世代 1.8GB（同一ディスクで DR 無力）
   - `kanae_daily.py` 毎時 cron が **26日間 Errno 2 で失敗し続けている**
     （2026-06-08 に source 削除済み、`&&` 短絡で KV push も一度も未実行）
   - `kanae-scheduler.service`（node 常駐）は happy-server RPC 全滅 +
     参照エンティティ削除済みで**二重に死亡**
   - `mcp-brave-search.service` が孤児 npx **19個 + swap 1.3GB** をリーク中
4. **Chroma の実態**: 40,315 ベクトル中、本番 recall に使うのは
   kanae_memories_safe=15,011 のみ。history_transcripts=22,153 は毎ターン
   OpenAI embedding を回して肥大し続ける dead weight（`MEM0_HISTORY_EMBED_ENABLED=1`）
5. **embedded ストアは RAM を食っていない**: SQLite BM25 27MB / LanceDB 62MB は
   常駐プロセスなし。D1/Vectorize 化しても **RAM 解放ゼロ**で recall に
   ネットワーク往復が乗るだけ（4モデル全員が D1 化を棄却）
6. **VPS 錨の確定**: Discord Gateway（WS 常駐）+ Claude OAuth
   （TLS fingerprint / ヘッダ byte-parity / body 署名 / ~/.claude device 錨着 —
   いかなるプロキシも不可）+ admin コード実行環境。Cloudflare Containers は
   WS 活動が sleepAfter を更新しないバグ（issue #147）で長寿命 outbound WS 不可、
   DO outbound WS は hibernation 非対応。**bot 本体の移設先は存在しない**
7. **現況逼迫**: used 4.6GB / free 331MB / swap 3GB — 主因は上記の債務と
   開発セッションであり、本番 bot（RSS 217MB）ではない

## 所有者決定（2026-07-04）

- **music_analyzer / web_api (Socket.IO Web UI) / kanae-daily は機能ごと完全削除**
  - web_api の認証先 Next.js (kanae-web) は既にプロセス停止済みのデッド経路
  - ただし `/health` だけは外形監視 Worker が依存 → **削除前に移設必須**
  - kanae-daily は C2 で Pages 化した直後だが、データが 26日 stale で機能自体が
    死んでいたことが判明 → Pages/KV ごと撤去

---

## Phase 0: 止血（即日、Cloudflare 不要）

swap 3GB の主犯を止める。全て停止系で、削除はまだしない。

| # | 作業 | 効果 |
|---|---|---|
| 0-1 | `mcp-brave-search.service` 停止・disable（孤児 npx 19個ごと reap） | swap ~1.3GB 解放 |
| 0-2 | `kanae-scheduler.service` 停止・disable（二重死亡確認済み） | 常駐 node -28MB |
| 0-3 | crontab から kanae_daily 行削除（26日間失敗中） | 毎時の失敗 spawn 消滅 |
| 0-4 | `/etc/cron.d/mem0-backup` 停止（backup_mem0.sh） | **毎日 03:00 の bot ダウンタイム根絶** |
| 0-5 | `MEM0_HISTORY_EMBED_ENABLED=0`（削除より先に蛇口を閉める） | per-turn OpenAI 往復消滅 + Chroma 肥大停止 |
| 0-6 | baseline 計測: PSS / MemAvailable / PSI / swap in-out を記録 | Phase 1 以降の効果測定の基準 |

**0-4 の前提**: C1 の無停止 snapshot_state.sh + R2 mirror が正のバックアップ経路に
なっていること（済）。ただし oracle 指摘のとおり **R2 からの restore test**
（tar 展開 → sqlite integrity → Chroma 起動）を一度やってから旧経路を殺す。

## Phase 1: 削除と債務返済（1〜2日）

| # | 作業 | 詳細 |
|---|---|---|
| 1-1 | `/health` 移設 | web_api.py から bot 本体（main_async 内の極小 aiohttp、:3002 維持）へ。外形監視の probe が途切れないことを確認してから 1-2 へ |
| 1-2 | web_api + Socket.IO + vscode_sync 削除 | 応答経路の二重実装（bot.py / web_api.py）も同時解消。kanae-web 694MB はアーカイブ |
| 1-3 | music_analyzer 削除 | services/music_analyzer/ + bot.py の呼び出し + テスト |
| 1-4 | kanae-daily 全撤去 | frontend/（node_modules 123MB 含む）、Pages プロジェクト、KV namespace、kanae_daily_push.sh、kanae-scheduler ディレクトリ（.mjs 9本） |
| 1-5 | .state 掃除 | バックアップ8世代 1.8GB 削除 + kanae_self 死体 159MB 削除（R2 に最終スナップショットを置いてから） |
| 1-6 | Chroma ダイエット | history_transcripts 22,153 + kanae_memories_infer 3,151 を落とし、**新 persist dir に kanae_memories_safe のみ再構築**（HNSW tombstone 回避）。ゲート: `eval_mem0_recall_fixed.py --compare` 30本回帰 + no-hit 率。通過後 MemoryHigh=250M/Max=400M に絞り、実測を見て段階的に縮小 |
| 1-7 | brave 切替完了 | bot config + Claude Code .mcp.json を Workers MCP（C3 デプロイ済み）へ。ローカル unit は Phase 0 で停止済み → 廃止 |
| 1-8 | 死蔵掃除 | toeic-reminder / kanae_self 系 migrate・audit / backfill 群 / iran-monitor / disabled MCP unit 6本 / htmlcov / crontab 孤立コメント |

**Phase 1 完了時の見込み**: swap 常用ゼロ、ChromaDB ~150-250MB 圏、
毎日ダウンタイム根絶、失敗し続ける cron ゼロ、bot プロセスから
web_api/music_analyzer のコードパスが消えて薄くなる。

## Phase 1.5: 開発セッション対策（oracle 指摘の格上げ）

真の RAM 主犯は本番ではなく開発。**Cloudflare より先に運用設計の問題**。

- Claude Code 用共有 MCP の HTTP 一本化を徹底（stdio 重複 spawn の根絶）:
  - brave/tavily/context7 → Workers MCP（1-7 の横展開）
  - oracle / second_opinion / mem0 → VPS 常駐 1 プロセスの streamable HTTP 共有
    （サブスク CLI 枠・OAuth 依存で Workers 不可のもの）
- 並行開発セッションの本数を絞る運用（または dev 用の分離環境を検討）
- ~~cgroup memory limit~~ は bot 本体には設定済み。開発セッション側は
  ユーザー slice への MemoryHigh 設定を検討

## Phase 2: 選択的 Cloudflare 化（1〜2週、効果は限定的と認識した上で）

| # | 作業 | 判定 |
|---|---|---|
| 2-1 | Puppeteer → Browser Rendering | 妥当（4モデル一致）。table/math renderer の内部差し替え、呼び出し側無変更。効果は常駐でなく**数百MB スパイクの消滅**。in-stream 描画のレイテンシ目視ゲート付き。ローカル Chromium 627MB + mermaid-cli 304MB のディスクも回収 |
| 2-2 | vi-signal / intervention → Cron Triggers | 原理的に容易（OAuth 非依存・通知は Discord REST/Webhook 済み）。ただし PSS 0.3〜4MB なので**効果はプロセス数と運用一元化のみ**。yfinance の Workers egress IP が唯一の実測ゲート。ダメなら VPS cron 化（常駐→cron だけでも十分） |
| 2-3 | 通知 Worker（軽量版） | 意見割れ（GPT/DeepSeek 賛成・Gemini 時期尚早）。**Webhook proxy + リトライ + 簡易レート制御だけの最小実装**で作る。Queues/D1/テンプレ基盤は入れない。全 cron/監視の通知先をここに一本化 |
| 2-4 | 残り API プロキシ MCP → Workers | tavily/context7/nikkei/polymarket（純プロキシ、C3 の型の横展開）。x-scraper は egress IP で impossible 確定 → VPS 残留 |

## Phase 3: mem0 ベクトル層 — 「予定された移行」ではなく gated experiment

**デフォルトはやらない。** Phase 1 後に以下が全て満たされるなら不要（oracle の条件）:

- steady state で swap-in/out がない
- MemAvailable ≥ 1.5〜2GB
- Chroma PSS が 150〜250MB 圏
- recall p95 悪化なし・毎日ダウンタイム消滅

逼迫が残る場合も、custom VectorStore 開発（数週間）より
**VPS 16GB への縦スケール（月数ドル）が SRE 的に正しい**可能性を先に検討する。

それでも Vectorize をやる場合の設計（4モデル一致で (b)→(a) の順）:
- (a) Python custom VectorStoreBase で Vectorize HTTP 直叩き（(c) TS 移設は棄却 —
  二重保守 + recall クリティカルパスに別ランタイム挿入）
- metadata を 10 キーに正規化: tenant/collection/visibility/user_id/channel_id/
  memory_type/status/created_day/importance_bucket + 残りは payload 側へ
- shadow mode（デュアルライト → recall 比較）→ 切替。切替後も Chroma を
  2週間 read-only 温存してロールバック可能に
- 必須ゲート: recall 回帰 + p95 +100〜150ms 以内 + Vectorize down 時の
  degraded mode 定義

## やらないリスト（4モデル合議で確定）

| 案 | 棄却理由 |
|---|---|
| BM25 SQLite → D1 | embedded で RAM ゼロ。移行は RAM 解放ゼロ + レイテンシ追加のみ。R2 スナップショットで耐久性は足りる |
| LanceDB (invest_kb) → Vectorize | 同上 + embedder 空間互換の問題（前版・発見8） |
| bot 本体 → Containers | WS sleepAfter バグ + 24/7 課金で VPS より高い |
| セッション状態 → KV/DO | sessions.json 429B。シングルプロセスに外部 state store は往復レイテンシの追加だけ |
| history JSONL → D1/R2 常用 | replay クリティカルパス + 追記型 FS が最適（前版判断を走査が裏付け） |
| Queues 導入 | mem0 ingest は fire-and-forget 済みで詰まっていない。使う理由が生まれてから |
| mem0 ごと TS Worker 化 | 「memory service を複数クライアントに提供する」要件が出るまで過剰 |
| AI Gateway | 従量 API（Gemini/OpenAI/DeepSeek）のみ可だが、music_analyzer 削除で主要ユースケースが消えた。残りは急がない |

## 高可用性について（反証として成立する唯一の条件）

VPS 単一障害点を本気で許容できないなら、答えは Cloudflare 移行ではなく:
cold standby VPS + R2 restore runbook + ~/.claude device 錨の復旧手順 +
Discord session 復帰手順。現状は「外形監視で気づける + R2 から復元できる」
まで整備済みで、個人 bot としては十分と判断（先輩の意向次第で Phase 4 として起案）。

## KPI（Phase 0/1 の前後で計測）

- MemAvailable / swap in-out / PSI some-avg10（baseline: free 331MB, swap 3GB）
- chromadb-server PSS（baseline: ~330MB）
- プロセス数・systemd unit 数・cron 失敗率（journal ベース）
- mem0 recall p50/p95 + 30本回帰の合格維持
- 月額コスト: Workers Paid $5 + Browser Rendering 実測（試算: 合計 $5〜15）

## 実施順序と見積り

| Phase | 内容 | 工数 | 主効果 |
|---|---|---|---|
| 0 | 止血（停止系のみ） | 半日 | swap -1.3GB, 毎日ダウンタイム根絶 |
| 1 | 削除と債務返済 | 1〜2日 | ディスク -4GB 超, Chroma -100〜180MB, コードベース縮小 |
| 1.5 | 開発セッション MCP 一本化 | 1日 | 開発時の重複 spawn 根絶 |
| 2 | Browser Rendering + Cron Triggers + 通知 Worker | 1〜2週(散発) | スパイク消滅, 運用一元化 |
| 3 | mem0 Vectorize（条件ゲート） | やらない見込み | — |

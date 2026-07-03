# Cloudflare オフロード計画 — リソース効率の抜本改革（2026-07-04）

## 目的

8GB RAM の単一 VPS に「なんでも常駐」させている現状を、Cloudflare のサーバーレス
基盤（Workers / D1 / Vectorize / R2 / Browser Rendering / Pages / Cron Triggers）へ
オフロードし、VPS には **Discord Gateway 常駐という本質的に常駐が必要なものだけ**を
残す。cloudflared tunnel は既に稼働しており、Cloudflare アカウント運用は実績あり。

## 現状実測（2026-07-04 03:45 JST）

### 常駐メモリマップ（本番系のみ、開発用 Claude Code セッション除く）

| プロセス | RSS | 分類 |
|---|---:|---|
| ChromaDB server | **402MB** | データストア（mem0 バックエンド） |
| mcp_mem0_server ×2 | **268MB+** | MCP（Claude Code 用） |
| discord-bot 本体 | 258MB | **本体（残留）** |
| oracle-mcp ×2 | ~208MB | MCP（外部 API プロキシ） |
| second_opinion_mcp ×2 | ~190MB | MCP（外部 API プロキシ） |
| node tsx (/app remote-opencode) + docker/containerd | ~250MB | コンテナ基盤 |
| kanae-daily-viewer（Vite preview） | ~100MB | **フロント配信のためだけの node 常駐** |
| brave-search 共有 MCP | 86MB | MCP（API プロキシ） |
| caddy + cloudflared + fail2ban | ~100MB | 配信・トンネル |
| vi-signal / intervention scheduler | ~80MB | 定期実行 |

→ **本体 258MB に対し、周辺が 1.7GB 前後**。さらに応答毎に npx で stdio MCP
（brave/tavily/x-scraper 等）を spawn するため、起動レイテンシ（1〜3秒）と
メモリスパイクが応答毎に発生。Puppeteer レンダリング（テーブル/数式画像）は
オンデマンドで Chrome を起こす（数百MB 級スパイク、同時1プロセス制限で直列化中）。

### データストア

| ストア | サイズ | 現在の場所 |
|---|---:|---|
| ChromaDB（mem0 ベクトル） | 323MB | ローカル HTTP サーバー |
| history JSONL（会話履歴正本） | 76MB | ローカル FS |
| invest_kb（LanceDB） | 62MB | ローカル FS |
| history_bm25.sqlite（FTS5） | 17MB | ローカル SQLite |
| mem0_bm25.sqlite（FTS5） | 6.5MB | ローカル SQLite |
| mem0_graph.sqlite | 3.7MB | ローカル SQLite |
| sessions.json / runtime_state.json | <1MB | ローカル FS |

## 移行マッピング（効果順）

### ★1 MCP 群 → Workers 上のリモート MCP（効果: 常駐 -750MB + spawn レイテンシ消滅）

Cloudflare は remote MCP サーバーのホスティングを公式サポート（Agents SDK /
McpAgent、streamable HTTP）。**API プロキシ型 MCP は Workers の理想形**
（リクエスト時のみ起動・課金、常駐ゼロ）。bot 側は `_shared_mcp_http()`（config
実装済み）で HTTP type MCP を既に扱えるため、**bot のコード変更は config の
接続先差し替えだけ**。

| MCP | 中身 | 移行性 |
|---|---|---|
| brave-search | Brave API プロキシ | ◎ 公式 TS 実装をそのまま Workers 化可能 |
| tavily | Tavily API プロキシ | ◎ 同上 |
| second_opinion | GPT/Gemini/DeepSeek への fan-out | ◎ Python→TS 書き直し（ロジック単純: 3 API 並列呼び） |
| oracle | OpenAI Responses API プロキシ | ○ TPM クランプのローカルパッチを移植する必要 |
| context7 / scholarly / google-maps | API プロキシ | ◎ |
| x-scraper | スクレイピング | △ 対象サイトの bot 対策と egress IP 次第。実測してから判断 |
| mcp_mem0_server | mem0 読み書き | ★3 の Vectorize 移行後なら Workers 化可能。それまでは VPS |
| nikkei-indexes / yfj-quote / minkabu-fx / polymarket | 市場データ取得 | ◎（スクレイピング成分は x-scraper と同じ留意） |

**留意**: Workers 無料枠は 10ms CPU/リクエスト（Paid で 5分 wall / 30s CPU）。
API プロキシは I/O 待ちが支配的なので CPU 制限には当たらない。

### ★2 Puppeteer レンダリング → Browser Rendering API（効果: Chrome スパイク消滅）

`utils/table_renderer.py` / `math_renderer.py` / `browser_pool.py` の Puppeteer を
Cloudflare Browser Rendering（Paid: 600 req/min、同時ブラウザ制限あり）の
screenshot REST API に置換。HTML を POST して PNG を受け取る形なので、
`render_table_image` / `render_math_image` の内部実装差し替えで済む
（呼び出し側は無変更）。「同時1プロセス制限」による描画直列化も解消。
ローカル Chromium / puppeteer-config / render_semaphore が全部消える。

### ★3 ChromaDB → Vectorize（効果: 常駐 -402MB、ただし要 PoC）

**mem0 は Cloudflare Vectorize を vector store として公式サポート**
（docs.mem0.ai/components/vectordbs/dbs/vectorize）。`_build_config()` の
vector_store provider を差し替える。

- **最大の懸念はレイテンシ**: 現在 localhost ChromaDB（~ms）→ Vectorize は
  ネットワーク往復（東京 PoP で +30〜80ms/クエリ見込み）。smart 検索は
  multi-query 並列なので壁時計への影響は限定的だが、**recall 品質・レイテンシの
  実測 PoC を必須ゲートにする**（scripts/eval_mem0_recall_fixed.py がそのまま使える）
- Python 版 mem0 SDK の Vectorize provider 対応バージョン確認が最初のタスク
  （docs の例は TS。Python 側に無ければ mem0 の VectorStoreBase 実装は薄いので自作可）
- 移行はデュアルライト → recall 比較 → 切替の3段（P4a シャドーモードと同じ手法）

### ★4 SQLite 群 → D1（効果: 中。バックアップ・耐久性の改善が主）

D1 は SQLite 互換で **FTS5 対応確認済み**、サイズ上限 10GB/DB、
料金は rows read $0.001/M・write $1.00/M・storage $0.75/GB-月。

| DB | 移行先 | 備考 |
|---|---|---|
| mem0_bm25.sqlite | D1 | FTS5 クエリそのまま。書き込みは ingest 時のみで安い |
| history_bm25.sqlite | D1 | 同上（per-turn 書き込みが rows written 課金に乗る点だけ試算） |
| mem0_graph.sqlite | D1 | 小規模・低頻度 |
| dgm.db ほか | 移行しない | 休眠データ |

**注意**: bot からの D1 アクセスは HTTP API（or Worker 経由）でレイテンシが乗る。
BM25 は recall のクリティカルパスなので、★3 と同じく実測ゲート。
「ローカル読み・D1 は非同期レプリカ」というハイブリッド（読みは速いまま、
耐久性だけ D1）も選択肢。

### ★5 kanae-daily → Pages（効果: node 常駐 -100MB、caddy 設定縮小）

Vite preview サーバーの常駐は Pages（静的 + Functions）の教科書的置換対象。
`vite build` → Pages デプロイ。データ（.state/kanae_daily.json）は Worker が
R2 か bot の Web API から読む形に。バックログ P1「kanae-daily API が Vite
middleware 依存」もこれで同時解消。

### ★6 バックアップ / スナップショット → R2（効果: 耐久性。VPS 全損への備え）

`~/.local/state/discord-bot/refactor-snapshots` と weekly backup の同期先を R2 に
（rclone or aws-cli S3 互換）。現在バックアップは同一マシン内のみ = VPS 全損で
記憶が全ロスする構成。**費用対効果が最も高い1手**（R2 は egress 無料、
~500MB なら月数円）。

### ★7 スケジューラ → Cron Triggers（効果: 小〜中）

vi-signal / intervention-scheduler / weekly_mem0_compact は「時刻起動 → データ取得
→ 判定 → Discord Webhook 通知」。Discord への通知を **Webhook** にすれば bot 非依存で
Workers Cron に載る。ただし mem0_compact はローカルデータ依存なので ★3/★4 完了後。

## 残すもの（VPS 残留）と、その先の選択肢

- **discord-bot 本体**: discord.py の Gateway（WebSocket 常時接続）は Workers の
  実行モデルに合わない。残留が正解。
  - 将来の選択肢A: **Cloudflare Containers**（Workers Paid、コンテナを
    リクエスト/手動起動で課金）— ただし Discord bot は 24/7 稼働なので
    常時起動課金になり、小型 VPS より高くつく可能性が高い。現 VPS 継続が合理的
  - 将来の選択肢B: Gateway 部分だけ残して application command 化（HTTP
    interactions endpoint は Workers で受けられる）— メンション会話 UX を
    捨てることになるので**非推奨**（かなえの会話体験が本体）
- **OAuth 認証（Anthropic サブスク枠）**: credentials はこのマシンの
  ~/.claude に紐づく。移動しない

## やらない方がいいもの（検討して棄却）

| 案 | 棄却理由 |
|---|---|
| history JSONL の D1 化 | 会話 replay のクリティカルパス + 追記型で FS が最適。R2 バックアップで十分 |
| invest_kb の即時 Vectorize 化 | ★3 の mem0 本体で PoC してから。LanceDB はローカルで問題を起こしていない |
| bot 本体の Containers 移行 | 24/7 課金で VPS より高い。メリットはポータビリティだけ |
| Durable Objects での状態管理 | bot がシングルプロセスな限り、追加する価値がない |

## 実施フェーズ案（依存順・低リスク順）

1. **C1: R2 バックアップ**（半日）— 即効・無リスク。snapshot_state.sh に R2 同期を追加
2. **C2: kanae-daily → Pages**（半日〜1日）
3. **C3: brave/tavily 等の API プロキシ MCP を Workers 化**（1〜2日）— bot は
   config の `_shared_mcp_http()` 差し替えのみ。ローカル systemd unit を順次廃止
4. **C4: Browser Rendering 置換**（1〜2日）— render_semaphore / browser_pool 削除
5. **C5: second_opinion / oracle の Workers 化**（1〜2日）— Claude Code の
   .mcp.json を リモート URL に
6. **C6: Vectorize PoC → mem0 移行**（3〜5日 + 観察）— recall 回帰30本 +
   レイテンシ実測がゲート。シャドー（デュアルライト）→比較→切替
7. **C7: BM25 の D1 化 or ハイブリッド**（2〜3日）— C6 の実測結果を見て判断
8. **C8: スケジューラ Cron Triggers 化**（1日）

C1〜C5 完了時点の見込み: **常駐メモリ -900MB 前後（ChromaDB 除く周辺をほぼ全廃）+
応答毎の npx spawn 消滅**。C6 まで行けば ChromaDB の 402MB も消え、VPS は
「bot 本体 + OAuth + JSONL」だけの ~500MB 構成になる。

## コスト概算（Workers Paid $5/月 前提）

- Workers リクエスト: MCP プロキシ利用は月数千〜数万リクエスト → 込み枠内
- Browser Rendering: 描画は1日数十回 → Paid 込み枠内に収まる見込み（要実測）
- Vectorize: ~10万ベクトル・クエリ月数万 → 数ドル以下
- D1 / R2: ストレージ数百MB → $1 未満
- **合計: 月 $5〜10 程度**で周辺ワークロードが全部サーバーレス化・多重化される

## 検証ゲート（各フェーズ共通）

- mem0 系: `scripts/eval_mem0_recall_fixed.py --compare`（30クエリ回帰）+
  レイテンシ計測（mem0_search_latency_ms ログの前後比較）
- レンダリング系: characterization の table/math フロー + 目視
- MCP 系: bot 経由の実呼び出し + smoke_bot.sh
- 監視: error_rate_monitor.sh（journald）で 24h 観察、切替は feature flag
  （env の接続先 URL）で即時ロールバック可能に

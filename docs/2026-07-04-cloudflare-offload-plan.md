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

---

# SRE 追補 — 詳細走査で見つかった追加オフロード候補（2026-07-04 第2弾）

深掘り走査（crontab 全数 / docker 実測 / 公開面 / 監視経路 / シークレット管理）で
見つかった追加項目。**発見の半分は「オフロード」以前の即時修正すべき運用債務**だった。

## 発見1: happy-server スタック — 隠れた最大の常駐（Docker 4コンテナ、~300MB + 基盤250MB）

`api.haroin57.com`（Caddy 公開）の実体は happy-server（Claude Code モバイル
クライアント同期サーバー）で、**Postgres 16 + Redis + MinIO + アプリ**を Docker で
セルフホストしている（5週間連続稼働、実測 ~300MB + docker/containerd 基盤 ~250MB）。

**これは discord-bot より大きい「もう一つのプロダクト」を1台に同居させている**構図。
オフロード先マッピング:
| コンポーネント | 移行先 | 備考 |
|---|---|---|
| happy-server アプリ | Workers（TS 製なら高適合）または Containers | 要: リポジトリの実装確認 |
| Postgres | D1（スキーマ規模次第）or Hyperdrive + 外部 PG | セッション同期程度なら D1 で足りる可能性大 |
| Redis | Workers KV / Durable Objects | pub-sub 依存なら DO |
| MinIO (S3) | **R2（完全互換、egress 無料）** | 最も自明な置換 |

これ一式が抜けると **Docker/containerd ごと消せる**（-550MB 級 = bot 本体の2倍）。
※ happy の公式クラウドを使う選択肢もある（セルフホストの理由を先に確認）。

## 発見2: ゾンビ cron + 死んだ tunnel ingress（即時修正、コストゼロ）

- **cron 3本が存在しないローカル Workers（:8787）を叩き続けている**
  （毎分1本 + 6時/18時）。8787 は past の `wrangler dev`（ashitabo-api）の残骸で、
  現在 LISTEN なし。**毎分 curl 失敗 ×5週間 = 誰も気づかない監視の死角**の実例
- **cloudflared tunnel の `ashitabo-api.haroin57.com` ingress も死んだ :8787 を指してる**
  （公開ホスト名が 502 を返し続けている状態）
- → ashitabo-api は本来 Workers アプリ（wrangler 前提の作り）なので、
  **Cloudflare に本デプロイして Cron Triggers を設定すれば、cron 3本と tunnel
  ingress と「ローカルで Workers をエミュレートする」倒錯が全部消える**。
  これが今回の走査で一番象徴的な発見: *Workers として書かれたものをローカルで
  動かしていた*
- timetable.haroin57.com（:8000）も LISTEN 確認が必要（同類の疑い）

## 発見3: 監視の共倒れ構造 → 外形監視のアウトソーシング

現在の監視は全て**監視対象と同じマシン内**にある:
- error_rate_monitor.sh（journald 読み）→ このマシンの cron
- アラート通知 = Discord bot 自身 → **bot が死ぬと「bot が死んだ」通知も死ぬ**
- /health エンドポイントは localhost:3002 のみ（外部から到達不能）

**オフロード**: Workers Cron（1分毎）で外から生存確認する外形監視を置く:
1. cloudflared tunnel 経由で `/health` を公開（Cloudflare Access で保護）
2. Workers が定期 probe → 失敗 N 回連続で **Discord Webhook**（bot 非依存の通知
   経路）に알림。Webhook は bot プロセスと独立なので共倒れしない
3. 同じ Worker に `error_rate` の push 先（Analytics Engine）を持たせれば、
   journald 依存の監視も段階的に外へ出せる

実装は Workers 1本 + Webhook URL 1個。**C1（R2 バックアップ）と並ぶ即効枠**。

## 発見4: AI Gateway — LLM 呼び出しの可観測性を無料で externalize

Cloudflare AI Gateway は Anthropic/OpenAI/Google の前段プロキシとして
**トークン・コスト分析 / キャッシュ / レート制御 / リトライ / フォールバック**を提供
（BYOK も、ヘッダ素通しも可）。現在 bot は usage telemetry を自前実装
（_log_turn_usage / MEM0_METRICS の grep 集計）しており、これを外に出せる。

**適合性の評価（重要な留意）**:
- ◎ **Mem0 の抽出/rerank（Haiku）、embedding（OpenAI）、SecondOpinion の API 直
  フォールバック、music_analyzer（Gemini/OpenAI）**: API キー認証の従量呼び出し。
  AI Gateway を通す価値が高い（コスト可視化 + 429 リトライ + キャッシュ）
- ✗ **bot 本体の Anthropic 呼び出し**: OAuth サブスク枠 + CLI 偽装ヘッダで
  subscriber tier を維持している特殊経路。**ゲートウェイを挟んで経路特性が変わると
  tier 判定を壊すリスク**があるため対象外とする（ここは触らない）

## 発見5: シークレット管理（.env 平文 40変数、うち KEY/TOKEN/SECRET 10個）

.env 平文 + ファイルパーミッション頼み。オフロード自体は必須ではないが、
Workers 移行（C3/C5）に伴い **API キー類は Workers Secrets へ移る**ので、
移行済みキーを .env から順次削除していく運用を C3 以降の各フェーズに含める。
VPS 残留分（DISCORD_TOKEN / OAuth credentials）は現状維持で妥当
（Cloudflare に出すものではない）。

## 発見6: 定期ジョブの全数マッピング（crontab + systemd timer 合算）

| ジョブ | 現在 | オフロード判定 |
|---|---|---|
| ashitabo-api の cron 3本 | **死んでる** | Workers Cron Triggers へ（発見2） |
| mem0_maintenance（7時/19時） | ローカル venv | △ mem0 データがローカルの間は残留。C6 後に Workers 化可能 |
| kanae_daily.py（毎時） | ローカル venv | △ Claude 呼び出し(OAuth)依存 → 残留。ただし出力 JSON の配信は Pages(C2) 側へ |
| monthly_kanae_audit | ローカル | 残留（月1、軽量） |
| vi-signal / intervention scheduler | systemd 常駐 | ○ C8: 通知を Webhook 化すれば Workers Cron へ（常駐→ゼロ） |
| weekly_mem0_compact / backup_state | ローカル cron | △ データ移行(C6/C7)後に判断。backup は C1 で R2 同期を先行 |
| sync-client-id.sh（6時間毎） | free-code | 残留（ローカル Claude CLI 依存） |
| error_rate_monitor | ローカル | 発見3 の外形監視 Worker に吸収 |

## 発見7: 公開面の Cloudflare 集約（Caddy 廃止の道筋）

現在: Caddy（origin cert 手動管理）+ cloudflared tunnel の2系統が併存。
- api.haroin57.com（happy-server）→ 発見1 の移行で Caddy の存在理由が消える
- kanae-daily → C2 で Pages へ
- 残る localhost サービス（web_api:3002 等）は tunnel ingress に一本化し、
  **Cloudflare Access（ゼロトラスト）で保護** — fail2ban + origin cert 管理の
  仕事が Cloudflare 側に移る

## 発見8: embedding の Workers AI 化は「やらない」（棄却の明文化）

OpenAI text-embedding-3-small → Workers AI（bge-m3 等）はコスト面で魅力だが、
**既存 323MB のベクトル資産と互換がない**（モデルが違えば空間が違う）ため、
全再埋め込み + recall 品質の再検証が必要。embedding 費用は現状月数十円レベルで
移行コストに全く見合わない。**C6（Vectorize 移行）でも embedder は OpenAI のまま**
とする。

## 更新後の優先順位（SRE 追補込み）

| # | 項目 | 効果 | 工数 |
|---|---|---|---|
| C0a | **ゾンビ cron 3本削除 + ashitabo-api を Workers 本デプロイ + tunnel ingress 修正**（発見2） | 運用債務解消 | 半日 |
| C0b | **外形監視 Worker + Discord Webhook**（発見3） | 監視の共倒れ解消 | 半日 |
| C1 | R2 バックアップ | DR | 半日 |
| C2 | kanae-daily → Pages | -100MB | 半日 |
| C3 | API プロキシ MCP → Workers | -400MB + spawn 消滅 | 1-2日 |
| C4 | Browser Rendering | スパイク消滅 | 1-2日 |
| C5 | second_opinion / oracle → Workers | -400MB | 1-2日 |
| C6 | ChromaDB → Vectorize（PoC ゲート付き） | -402MB | 3-5日 |
| C7 | BM25 → D1 or ハイブリッド | 耐久性 | 2-3日 |
| C8 | スケジューラ → Cron Triggers | -80MB | 1日 |
| C9 | **happy-server スタック移行**（発見1、要スコープ調査） | **-550MB + Docker 全廃** | 別途見積 |
| C10 | AI Gateway（従量 API 呼び出しのみ、発見4） | 可観測性 | 1日 |

全完了時の VPS 想定: **discord-bot 本体 + OAuth + JSONL + ローカル Claude CLI 系のみ
（~600MB / 8GB）**。Docker・Caddy・ChromaDB・Node 常駐が全廃され、監視は外形化、
バックアップは R2、公開面は Cloudflare Access に統一される。

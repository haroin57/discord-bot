# メモリ逼迫の抜本対策 — 監査結果と再設計・実装計画（2026-10-16）

対象ホスト: Ubuntu 25.04 / 8GB RAM / swap 11GB（zram 3G + swapfile 2G + 6G）/ 4 vCPU。
症状: swap を使い切って Discord gateway heartbeat が数十秒ブロックされ、最悪はホストが応答不能になる。

この文書は (1) 実測にもとづく原因の確定、(2)「一から作り直せるなら」の目標アーキテクチャ、
(3) 段階的な実装計画（Phase 0 は本ブランチ `agent/memory-diet` で実装済み）をまとめる。
コード監査の詳細は Codex（GPT 系）の独立監査と Explore エージェントのインベントリを突き合わせた。

## 1. 実測（2026-10-16 03:50〜04:15 JST）

`free -m`: used 5.4G / available 2.5G、**swap 6.7G 使用**（zram 3G は満杯）。

### 1.1 消費の内訳（cgroup の RAM + swap、PSS）

| 順位 | 消費者 | RAM | swap | 性質 |
|---|---|---|---|---|
| 1 | `chromadb-server.service`（`chroma run`, Rust 1.x） | 250MB | **3,323MB** | 長寿命リーク。データは 22,047 ベクトル（生 135MB, sqlite 73MB）しかない |
| 2 | `/tmp`（tmpfs = RAM/swap） | 2.3GB | ↑込み | `.pnpm-store` **1.3GB**、puppeteer プロファイル残骸 36 個 331MB、`claude-opus55-audit-cli` 246MB |
| 3 | 孤児 headless Chrome × 7 | **1,577MB (PSS)** | — | PPID=1。`app-org.chromium.Chromium-<pid>.scope` に自力で移って誰にも回収されない |
| 4 | Claude Code CLI セッション（user slice） | 約 730MB | 132MB | 開発用。対話中だけ |
| 5 | bot × 3（prod / kanae-claude / dev） | 250+284+86MB | 82+0+113MB | prod は MemoryPeak 1.57GB |
| 6 | `mem0-hook-daemon` | 312MB | — | Claude Code hook のためだけに Mem0 スタック全体 + v1 連想 index を常駐 |
| 7 | `mcp-mem0-shared` | 266MB | — | 同上（Mem0 スタックの重複） |
| 8 | 共有 MCP 群（oracle / papermcp / second-opinion / x-scraper / inshi-vault） | 計 約 130MB | 計 約 270MB | 小粒だがランタイムごとに固定費 |

「漏れ」（1〜3）だけで **約 7.4GB**。正規の常駐（4〜8）より大きい。
つまり問題の本質は「処理が重い」ことより、**所有者のいない資源が上限なしに積み上がる**ことにある。

### 1.2 根本原因

1. **Chrome の孤児化（構造的）**
   - `utils/_render_daemon.js` は puppeteer を WebSocket（`--remote-debugging-port=0`）で起動する。
     puppeteer は Chrome を detached（別 session / 別 process group）で起動する。さらに Chrome は
     起動時に systemd user bus 経由で自分専用の scope に移る。このため、親の node を SIGKILL しても、
     process group を kill しても、サービスの cgroup を止めても Chrome に届かない。
   - 再現手順は次のとおり。node を SIGKILL すると、ws モードでは Chrome が生存して孤児になる。
     `pipe: true` では CDP pipe が閉じて Chrome が自分で終了する（`/tmp/discord_bot_attachments/memdiet/pipe_probe.js`）。
   - 孤児の cmdline 末尾 `--no-sandbox --disable-setuid-sandbox --no-sandbox --disable-setuid-sandbox --disable-gpu --disable-dev-shm-usage`
     は `_render_daemon.js` の引数と一致する。起動元は BrowserDaemon で確定。user slice にいるので、
     Claude Code セッション内でのテスト・スクリプト実行（timeout kill / Ctrl-C）でも同じ経路で漏れる。
   - CLAUDE.md が案内する `mmdc` は **現在 Chrome 131 が見つからず起動に失敗する**（キャッシュには 147 のみ）。
     Mermaid は今は事実上壊れている。
2. **Chroma サーバーの長寿命リーク**
   - データ量に対して RSS+swap が 25 倍以上ある。`MemoryHigh=250M` が reclaim を強制し、
     `MemorySwapMax` は未設定（infinity）なので、OOM で落ちる代わりに swap へ押し出し続ける。
     7.5 日の連続稼働で 3.5GB に達した。
   - 2.2 万件規模に専用のベクトル DB サーバーは過剰。numpy の brute-force で約 130MB / 10ms 級。
3. **tmpfs に置くべきでないもの**: pnpm の content-addressable store（1.3GB）が `/tmp` にある。
   puppeteer プロファイルは異常終了のたびに残る。
4. **Mem0 スタックの重複ロード**: bot 各インスタンス、hook daemon、MCP server の計 5〜6 プロセスが、
   それぞれ mem0 SDK・chromadb client・OpenAI/Anthropic SDK・sqlite ストア・(v1 なら) 連想 index を持つ。
   hook daemon と MCP server はどちらも `MEM0_RETRIEVAL_MODE=v1` なので、v1 index（最大 10 万件取得 + numpy）を二重に持つ。
5. **補助的な要因**（Codex 監査）
   - Codex app-server にアイドル TTL がない。
   - いくつかの subprocess で、タイムアウト時に子孫を回収していない（free_code_bridge / scheduler / grep）。
   - Codex イベント Queue にバイト上限がない。
   - session tail にバイト上限がない。
   - turn outbox が全文を常駐する。
   - どれも単体では GB 級ではないが、「上限がない」点は同じ設計欠陥。

参考: 前回のダウン（10/08 07:46 → 15:35）の直前ログは `No space left on device` だった（ディスク / ログ起因）。
メモリだけでなくディスクも「上限なし」の同類として扱う。

## 2. 設計原則（作り直すなら）

1. **すべての資源に所有者と上限を持たせる**。プロセス・ファイル・キュー・キャッシュのそれぞれについて、
   「誰が回収するか」と「最大いくつ / 何 MB か」が言えないものは作らない。
2. **OS に回収させる構造を優先する**。後片付けコードより、親が死ねば子も死ぬ構造
   （pipe / PDEATHSIG / cgroup）の方が壊れにくい。reaper はあくまで最終保険。
3. **重い依存は 1 プロセスに集約し、他は薄いクライアントにする**（Mem0、ブラウザ）。
4. **ステートレスで I/O 待ちの処理だけを Cloudflare に出す**。gateway WebSocket、OAuth 資格情報、
   CLI/Bash ツール、ローカル FS 依存は VPS に残す（07-04 計画の判断を踏襲）。
5. **swap は緩衝材であって容量ではない**。全 unit に `MemorySwapMax` を設定し、溢れたら
   swap に沈めるのではなく再起動させる（systemd の Restart で回復する設計にする）。

## 3. 目標アーキテクチャ

```
                ┌──────────── Cloudflare ────────────────────────────┐
                │ Workers: second-opinion MCP / API プロキシ MCP     │
                │ Cron Triggers: intervention / vi_signal / status   │
                │ Browser Rendering: table（既存）, math/mermaid(任意)│
                │ R2: archive / backup（既存 Worker）                │
                └───────────────▲────────────────────────────────────┘
                                │ HTTPS
┌─────────────────────── VPS（定常 ≤ 3GB、swap ≒ 0）──────────────────────────┐
│ bot-prod / bot-kanae-claude（gateway + agent loop、重い import は遅延）      │
│     │ HTTP（127.0.0.1）                                                      │
│     ▼                                                                        │
│ kanae-memory（唯一の Mem0 所有プロセス: 埋め込みベクトル index + BM25 + fact）│
│     ├─ MCP endpoint（Claude Code 共有）  ├─ /hook/search（Mac hook）          │
│ render-daemon（Chrome 1 個、pipe 接続、idle 5 分で終了、cgroup 上限）         │
│ bot-dev（オンデマンド起動、アイドル停止）                                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

| コンポーネント | 現状 | 目標 | 期待効果 |
|---|---|---|---|
| Chrome | 呼び出しごと / 異常終了で孤児化 | pipe 接続の所有 daemon 1 個 + reaper | −1.6GB（増え続ける分を止める） |
| Chroma | 独立 Rust サーバー、swap 無制限 | Phase 0: swap 上限 + 定期再起動 → Phase 2: kanae-memory 内の埋め込み index（LanceDB か sqlite-vec） | −3.3GB → プロセス自体を廃止 |
| Mem0 スタック | 5〜6 プロセスで重複 | kanae-memory に集約、他は HTTP クライアント | −300〜600MB |
| hook daemon | Mem0 全スタック 312MB | stdlib のみの proxy | −約 300MB |
| /tmp | pnpm store / プロファイル残骸 | store は disk へ、tmpfiles.d で aging | −1.6GB |
| dev bot | 常駐 | オンデマンド | −200MB |
| MCP 群 | ローカル常駐 | ステートレス系は Workers | −100〜300MB |

**Vectorize ではなく埋め込み index を推奨する理由**: 07-04 の計画は Chroma → Vectorize（C6）だった。
今回の実測では、問題はベクトルの保存場所ではなく **Chroma サーバーというプロセスの寿命特性** だった。
データは 2.2 万件と小さく、Mem0 の検索は応答のクリティカルパス上にある（現状でも 700〜900ms）。
smart 検索は複数クエリを並列に投げるので、ネットワーク RTT がクエリ数ぶん乗る Vectorize は不利になる。
加えて Vectorize は D1/BM25 との整合も別途設計が必要になる。
埋め込み index は mmap ベースで RSS も小さく、レイテンシはむしろ改善する。
Vectorize は「VPS 全損時の耐久性」を主目的にするなら再検討する（R2 バックアップで代替可能）。

## 4. 実装計画

### Phase 0 — 漏れを止める（本ブランチで実装、即日）

| # | 変更 | ファイル | 検証 |
|---|---|---|---|
| 0-1 | render daemon を `pipe: true` に変更。Chrome PID を Python に通知し、異常系では node の process group と Chrome PID を両方 kill する。node は `start_new_session=True` で起動。OptimizationGuide / OnDeviceModel を無効化 | `utils/_render_daemon.js`, `utils/browser_backend.py` | 実 Chrome の結合テスト（node を SIGKILL → Chrome が消えること、正常 render → close で 0 個） |
| 0-2 | 孤児 Chrome reaper。PPID=1、`puppeteer_dev_chrome_profile-`、経過 5 分以上を TERM → KILL。生存プロセスが使っていない 1 時間超のプロファイルディレクトリも削除 | `scripts/reap_mcp_orphans.sh`（既存 cron */10 に相乗り） | `--dry-run` で 7 個を検出、本実行で 0 個 |
| 0-3 | Mermaid の安全ラッパー。最新 Chrome を自動解決、`pipe: true`、timeout + kill-after、プロファイル後始末。CLAUDE.md の案内を差し替え | `scripts/mmdc_safe.sh`, `config/puppeteer-mmdc.json`, `CLAUDE.md` | 実レンダリング成功、timeout 時に Chrome 残存 0 |
| 0-4 | hook daemon を stdlib のみの proxy に置き換え、検索は `mcp-mem0-shared` の `/hook/search` に寄せる（同一引数・同一フォーマット） | `services/mem0/hook_http.py`（新規）, `mcp_mem0_server.py`（登録 2 行）, `mem0_hook_daemon.py` | 単体テスト + 実 HTTP 往復で出力一致 |
| 0-5 | Chroma の swap 上限と毎日 05:30 の再起動 | `infra/systemd/chromadb-server.service.d/memory-guard.conf`, `infra/systemd/chromadb-restart.{service,timer}` | 適用後の MemorySwapCurrent を観察 |
| 0-6 | /tmp の衛生。puppeteer プロファイルの aging、pnpm store の移設手順 | `deploy/tmpfiles.d/discord-bot-tmp-hygiene.conf`, `scripts/host_tmpfs_hygiene.sh` | `systemd-tmpfiles --clean` dry-run |

Phase 0 の見込み: 定常で **−5GB 以上**（Chroma 3.3G + Chrome 1.6G + /tmp 1.6G + hook 0.3G の大半）。

**本番適用の実測（2026-10-16 10:59 JST、`apply_memory_diet_host.sh --with-pnpm`）**:
- swap: 7,825MB → 2,083MB
- available: 3,517MB → 4,784MB
- /tmp: 2.5GB → 511MB
- Chroma: swap 3.5GB → 0、RAM 75MB
- 孤児 Chrome: 7 → 0、プロファイル残骸 48 個を削除
- hook daemon の proxy 化はコードのデプロイ後に効く（−約 300MB 見込み）

### Phase 1 — 上限のない構造を潰す（1〜2 週間）

1. **kanae-memory サービス化**
   - `mcp_mem0_server.py` を唯一の Mem0 所有プロセスに昇格させる。
   - 内部 HTTP API（`/v1/search`, `/v1/add`, `/v1/core`）を追加する。
   - bot 側の `services/mem0` 呼び出しを `MEM0_BACKEND=remote` で HTTP クライアントに差し替える
     （feature flag、シャドー比較 → 切替）。
   - bot プロセスから mem0 SDK・chromadb の import を消す。
2. **遅延 import**
   - `services/claude_api/__init__.py` の再 export を遅延化する。
   - `services/mem0/assoc.py` の numpy を v1 実行時 import にする。
   - 起動時 RSS を CI で計測して回帰ゲートにする（`python -X importtime` + RSS 閾値）。
3. **subprocess の所有管理を一本化**
   - `subprocess_runner` の TERM → KILL → wait + process group の流儀に、
     `free_code_bridge` / scheduler / grep tool / codex client を寄せる。
   - Codex app-server にアイドル TTL（既定 15 分）を入れる。
4. **上限のあるキュー / バッファ**
   - Codex イベント Queue にバイト予算を設ける。
   - session tail の読み取りにバイト上限を設ける。
   - turn outbox は完了 payload を receipt に縮約し、未完了は SQLite で 1 件単位に読み書きする。
5. **全 unit に `MemorySwapMax` を設定**
   - bot には 256M、MCP 群には 64M などを割り当て、`ManagedOOMMemoryPressure=kill` と systemd-oomd を有効化する。
   - `mem_pressure_alert.sh` に「unit 別 RAM+swap 上位 5」を追記し、通知だけで原因が分かるようにする。

### Phase 2 — Chroma 廃止（Phase 1 の kanae-memory 内で）

1. まず `kanae_memories_safe` 21,017 件の中身を監査する。ADR の記録では移行時に約 860 件だったので、
   重複・履歴・migration 残骸の疑いがある。dedup して件数を正す。
2. 埋め込み index の PoC。候補は LanceDB（invest_kb で運用実績あり、mmap）と sqlite-vec（単一ファイル、brute-force）。
   Mem0 の vector store アダプタとして実装する。
3. デュアルライト（Chroma + 新 index）→ recall 回帰 30 本 + p95 レイテンシ比較 → 読み取り切替 → Chroma 停止。
4. ロールバック: 読み取りフラグを戻すだけで済むよう、デュアルライト期間を 1 週間取る。

### Phase 3 — Cloudflare オフロード（07-04 計画 C3/C5/C8 の残り）

- `second-opinion` MCP → Workers（純 API プロキシ）。
- `intervention_scheduler` / `vi_signal_scheduler` → Cron Triggers（通知は webhook）。
- math / mermaid の Browser Rendering 化は、Quick Actions の rate limit が許すなら任意で行う。
  ローカル daemon は fallback として残す。
- `x-scraper` / `oracle`（ブラウザ・cookie 依存）は VPS に残す。

### Phase 4 — 運用形態

- `discord-bot-dev` をオンデマンドにする。`systemctl start` で起動し、30 分アイドルで自己停止する。
- `hermes-discord-codex` の要否を棚卸しする。
- `user@1000` 配下の Claude Code セッション数に運用上の上限を設ける（herdr ペイン運用のガイド）。

## 5. メモリ予算（目標）

| 区分 | 上限 (MemoryMax) | swap 上限 |
|---|---|---|
| bot-prod | 900M | 256M |
| bot-kanae-claude | 900M | 256M |
| kanae-memory（Mem0 + index） | 600M | 128M |
| render-daemon（Chrome） | 600M | 0 |
| MCP 群（合計） | 400M | 128M |
| 開発セッション | 残り（約 3GB） | — |

## 6. リスクと対策

- **Chroma の毎日再起動で Mem0 検索が一時的に失敗する**: 2026-10-16 11:00 の本番適用で実測したところ、
  heartbeat 復帰は約 3 秒だったが、in-process で Mem0 を持つ旧 hook daemon は再接続まで約 1〜2 分 timeout（空ブロック）が続いた。
  bot 側は既存の init cooldown と空結果フォールバックで吸収し、エラーログは出なかった。
  時刻は 05:30（就寝誘導ルールにより haroin の利用が最も少ない帯）に置く。
- **hook proxy 化で MCP サーバーの負荷と RSS が増える**: v1 index は既に MCP 側でも構築済みなので増分は小さい。
  MemoryMax 450M は据え置き、超えたら Phase 1 の予算に従って引き上げる。
- **`pipe: true` と puppeteer の互換性**: mermaid-cli 同梱の puppeteer で実測済み。
- **作業ツリーの並行開発**: `/home/ubuntu/discord-bot` には別セッションの未コミット作業（tracked 84 ファイル + untracked 146）があり、
  稼働中の hook daemon / MCP はそのツリーのコードで動いている。
  本ブランチは dirty 側で変更のないファイルを中心に触り、`mcp_mem0_server.py` は末尾近くの 2 行だけにとどめて衝突を避けた。

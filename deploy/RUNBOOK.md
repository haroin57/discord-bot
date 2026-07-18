# discord-bot Operations Runbook

## Deploy

```bash
sudo -u kanae-deploy /home/ubuntu/discord-bot/scripts/bot-deploy.sh
```

The script fails closed on a dirty worktree or active rollback pin and holds the
same `flock` as auto-deploy and rollback. Before changing HEAD it requires the
staging receipt/readiness gate and a successful snapshot restore rehearsal. It
then builds immutable SHA-specific external generations, merges the exact
fetched commit object, atomically switches the single `current` symlink,
coordinately restarts every affected consumer, and accepts `GET /ready` only
when every consumer is active and release readiness reports the exact SHA.
An fsynced `activation.json` is written before the switch. A rerun resumes any
incomplete consumer sequence before evaluating rollback pins or Git no-op
conditions.

If smoke or another build hook dirties the merged checkout, the script
stores patches and non-ignored untracked files under
`/var/lib/discord-bot-deploy/<env>/quarantine/`, restores a clean blocked SHA,
switches to the verified old SHA, and leaves `rollback.pin` active.  Inspect the
quarantine before removing the pin.  Do not restart manually while HEAD still
equals the pin's `blocked_sha`.

## Rollback

```bash
sudo -u kanae-deploy /usr/local/libexec/discord-bot-rollback \
  pre-deploy-20260513-120000 <expected-old-sha> <blocked-release-sha>
```

Rollback requires the tag to resolve to the exact expected old SHA and HEAD to
equal either the exact blocked SHA or an already-restored expected old SHA. It
is idempotent after interruption and replaces an incomplete forward activation
with a rollback transaction. It writes
`/var/lib/discord-bot-deploy/prod/rollback.pin` before detaching HEAD and
restarting. Auto-deploy cannot overwrite that pin. After the incident is
resolved, verify that HEAD equals `target_sha`, the checkout is clean, and
`/ready.release_sha` equals `target_sha`; then remove the pin explicitly before
the next deploy.

```bash
sudo -u kanae-deploy git -C /home/ubuntu/discord-bot status --short
target_sha="$(sudo sed -n 's/^target_sha=//p' \
  /var/lib/discord-bot-deploy/prod/rollback.pin)"
test "$(sudo -u kanae-deploy git -C /home/ubuntu/discord-bot rev-parse HEAD)" \
  = "${target_sha}"
curl -fsS http://127.0.0.1:3002/ready | jq -e \
  --arg sha "${target_sha}" '.status == "ok" and .release_sha == $sha'
```

## Health Check

```bash
curl -f 127.0.0.1:3002/live | jq
curl -f 127.0.0.1:3002/ready | jq
curl -f 127.0.0.1:3002/health | jq  # compatibility alias for /ready
curl -f 127.0.0.1:3003/ready | jq   # staging identity
curl -f 127.0.0.1:3004/ready | jq   # Hermes identity
```

An unprovisioned checkout or required consumer is an auto-deploy failure and
triggers the deploy notification path. Planned absence must be declared with a
root-owned systemd drop-in setting `Environment=AUTO_DEPLOY_DISABLED=1`; no
implicit missing-service success path exists.

`/live` reports only HTTP-process liveness. `/ready` reports Discord, OAuth,
Mem0, host, and release identity readiness and returns 503 while degraded.
OAuth credential absence, unreadability, invalid/expired token content,
owner/mode violations, and unavailable/invalid release metadata all fail
closed. `build_sha` and `release_sha` are read from protected metadata at
process start, so moving
the checkout without restarting does not make an old process claim a new
release. `/health` preserves the old readiness route as an alias.
Startup also verifies `release-content.jsonl`, its metadata digest, every
regular `src`/`venv` file mode and SHA-256, root ownership, and the absence of
write permission. A modified generation returns release readiness failure.

```bash
target_sha="$(sudo -u kanae-deploy python3 -c \
  'import json; print(json.load(open("/var/lib/discord-bot-releases/prod/current/release.json"))["release_sha"])')"
curl -fsS http://127.0.0.1:3002/ready | jq -e \
  --arg sha "${target_sha}" '.status == "ok" and .release_sha == $sha'
```

## Source ACL and state boundary verification

Provision source traversal, OAuth credentials, and per-service state by
following `deploy/OPERATIONS_SECURITY.md`.  Before starting a revised unit,
verify the effective UID can traverse the mode-`0750` home directory and can
read but not write the checkout.

```bash
sudo -u kanae-bot test -x /home/ubuntu
sudo -u kanae-bot test -x /var/lib/discord-bot-releases/prod/current/venv/bin/python
sudo -u kanae-bot test -r /var/lib/discord-bot-releases/prod/current/src/main_async.py
! sudo -u kanae-bot test -w /var/lib/discord-bot-releases/prod/current/src/main_async.py
sudo -u kanae-bot test -w /var/lib/kanae-bot/state
! sudo -u kanae-mem0 test -w /var/lib/kanae-bot/state/sessions.json
sudo -u kanae-bot stat -c '%U:%G %a' \
  /var/lib/kanae-bot/.claude/.credentials.json
sudo -u kanae-bot test -r /var/lib/discord-bot-releases/prod/current/release.json
! sudo -u kanae-bot test -r /home/ubuntu/discord-bot/.git/HEAD
```

Expected credential output is `kanae-bot:kanae-bot 600`.  Expected state is
private mode `0700` with no `kanae-state` supplementary group.  Start in this
order: `chromadb-server`, MCP daemons, schedulers, `hermes-discord-codex`, then
`discord-bot`.  Verify every service before archiving the old checkout
`.state`.

## Logs

```bash
journalctl -fu discord-bot          # live stream
journalctl -u discord-bot -n 200    # last 200 lines
```

## Disaster Recovery

Backups are written to `/var/lib/discord-bot-backup/snapshots/` from the
root-owned `/etc/discord-bot/backup-state.inventory`. WAL/SHM physical churn is
not a retry condition; consistency is proven from each `.backup` result and its
logical digest. Any SQLite main-database path addition or deletion during the
attempt discards that attempt. Snapshot publication fsyncs the complete tree
and rename parents.

To restore:
1. Stop the service: `sudo systemctl stop discord-bot`
2. Identify the latest backup: `ls -lt /var/lib/discord-bot-backup/snapshots/ | head`
3. Copy the backup files back to their expected paths (sessions JSON, Mem0 DB, etc.)
4. Start the service: `sudo systemctl start discord-bot`
5. Confirm health: `curl 127.0.0.1:3002/health | jq`

## Cloudflare R2会話・状態アーカイブ

`services/r2_archive.py`はローカル保存後に`.state/r2_archive/pending/`へ永続スプールし、
バックグラウンドで非公開R2バケットへ送信する。R2停止中もbotの会話処理は継続し、
失敗項目は指数バックオフで再送する。全体復元用の`snapshots/*.tar.gz`は従来の
`scripts/snapshot_state.sh`が担当し、本機能は検索・個別取得しやすい次の構造を担当する。

```text
discord-bot/v1/history/events/<channel>/YYYY/MM/DD/<event>.json
discord-bot/v1/history/transcripts/<channel>.jsonl
discord-bot/v1/images/<channel>/YYYY/MM/DD/<message>/<direction>/<image>
discord-bot/v1/state/latest/<relative-path>
discord-bot/v1/state/manifests/YYYY/MM/DD/<timestamp>.json
```

- 状態確認: `python scripts/r2_archive.py status`。
- `.state`差分登録: `python scripts/r2_archive.py sync-state`。
- 送信待ち処理: `python scripts/r2_archive.py flush --limit 100`。
- 個別取得: `python scripts/r2_archive.py fetch 'r2://kanae-state-backup/<key>' --output /tmp/object`。
- Discord内の画像確認・取得: 管理者用`/r2recent`と`/r2fetch`。
- 即時状態同期: 管理者用`/r2sync`。
- 無効化: `.env`で`R2_ARCHIVE_ENABLED=false`。
- ローカル索引: `.state/r2_catalog.sqlite`。`objects`表で`channel_id`、`message_id`、
  `request_message_id`、`response_message_id`、`attachment_id`、`object_key`、`sha256`、
  `status`、`retry_count`を保持する。旧`.state/r2_archive/catalog.jsonl`は初回接続時に
  一度だけ移行し、以後はSQLiteを正本とする。

R2バケットは公開しない。画像の共有URLが必要な場合は、S3 API資格情報を別途発行して
短時間のpresigned GET URLを生成する。資格情報やpresigned URLはJSONLへ保存しない。

## Common Failures

| Symptom | First step |
|---|---|
| Crash loop | `systemctl status discord-bot` — check exit code and last 20 log lines |
| OAuth refresh failing | `journalctl -u discord-bot | grep -i oauth_manager` — look for WARNING/ERROR |
| Mem0 backend down | `journalctl -u discord-bot | grep WARNING` — Mem0 errors surface here |
| Health returns 503 | Check if web_api.py port 3002 is bound: `ss -tlnp | grep 3002` |
| Bot offline in Discord | Confirm TOKEN env is set: `systemctl cat discord-bot | grep Env` |

## Service Control

```bash
sudo systemctl status discord-bot
sudo systemctl restart discord-bot
sudo systemctl stop discord-bot
sudo systemctl start discord-bot
```

## Refactor Train Operations (P0.25 — docs/2026-07-03-grand-refactor-plan.md)

リファクタ期間中、state/session/fallback/mem0 を触る PR の本番反映は必ずこの順で行う:

```bash
# 1. 明示inventoryによる整合スナップショット + 復元リハーサル
sudo -u kanae-backup env \
  SNAPSHOT_STATE_INVENTORY=/etc/discord-bot/backup-state.inventory \
  SNAPSHOT_ROOT=/var/lib/discord-bot-backup/snapshots scripts/snapshot_state.sh
scripts/snapshot_state.sh --verify <snap-dir> # integrity_check まで通ることを確認

# 2. 再起動前スモーク（red なら再起動しない）
BOT_VENV=/var/lib/discord-bot-releases/prod/current/venv scripts/smoke_bot.sh

# 3. 再起動（深夜帯推奨）
sudo systemctl restart discord-bot

# 4. 観察期間（24〜72h）はエラー率ゲートを回す
scripts/error_rate_monitor.sh                 # 15分窓 / ERROR>5 で exit 1
# cron 例: */15 * * * * .../error_rate_monitor.sh || echo "breach $(date)" >> /tmp/bot_alerts.log
```

### 切り戻し

```bash
scripts/bot-rollback.sh refactor-base-20260703   # tag へ即時 rollback + restart
# .state が壊れた場合のみ: bot 停止 → snapshot から .state を戻す → 起動
```

### Feature flag 規約（リファクタ用）

- 新旧経路を切り替えるフラグは `REFACTOR_<AREA>_NEW=1` 形式の env で `config.py` の
  `_bool_env()` を使って定義する（例: `REFACTOR_SENDER_NEW`）
- デフォルトは**旧経路**。新経路が観察期間を通過したらデフォルト反転 → 次のフェーズで旧経路削除
- フラグの寿命は最長1フェーズ。恒久 config 化しない

## 外形監視（C0b, 2026-07-04）

bot と運命を共にしない監視経路。**bot 完全死でもアラートが届く**。

- probe: Cloudflare Worker `kanae-health-probe`（毎分 cron）→
  https://health.haroin57.com/health（cloudflared tunnel、/health のみ path 公開）
- 判定: 3回連続失敗で DOWN、2回連続成功で復帰（フラップ抑制）。HTTP 200 でも
  body の status≠"ok"（degraded）は失敗扱い
- 通知: Discord Webhook → haroin のサーバーの監視チャンネル
  （channel_id 1522684302687469749）。DOWN 継続中は 30分毎に再通知。
  Webhook URL は Worker の secret `WEBHOOK_URL`（差し替えは
  `wrangler secret put WEBHOOK_URL`）
- 状態確認: https://kanae-health-probe.haroin57.workers.dev/ （現在の KV 状態を返す。
  `service`=サービスUP/DOWN、`resources`=リソース閾値状態）
- ソース: ~/archives/cloudflare-workers-src-20260704/kanae-health-probe（wrangler プロジェクト）

### ホストリソース閾値監視（C0c, 2026-07-04）

`/health` の `subsystems.system`（psutil 由来）を同じ Worker が毎分チェック。
サービスの UP/DOWN とは独立した通知経路（🟡 WARN / 🟢 復帰）。

| メトリクス | 警告閾値 | 復帰閾値 |
|---|---|---|
| disk_pct | 85% | 80% |
| mem_pct | 90% | 85% |
| swap_pct | 50% | 40% |
| cpu_load_pct（load1/nproc×100） | 150% | 120% |

3回連続超過で WARN、2回連続回復で CLEAR（ヒステリシス幅でフラップ抑制）。
継続 WARN 中は 30分毎に再通知。

## journal ログ配信（C0d, 2026-07-04）

discord-bot の journal のうち WARNING 以上を haroin サーバーの
`#bot-journal-log`（channel_id 1522691767001157672）へリアルタイム配信する。

- デーモン: `scripts/journal_forward.py`、systemd unit
  `discord-bot-journal-forward.service`（`discord-bot.service` と同居、
  stdlib のみで venv 非依存）
- **選定基準は journalctl の `-p` ではない**: discord-bot は
  `services.structured_log.JsonFormatter` で1行1JSONのログを出しており、
  どのレベルも同じ syslog priority で出力されるため `journalctl -p warning`
  は常に0件になる（実測: 24hで177件のWARNINGがあるのに `-p warning` は0件）。
  正しくは journalctl をフィルタなしで流し、各行を JSON パースして
  `level` フィールドで WARNING/ERROR/CRITICAL を選定する（`select_line()`）
- バッチ処理: 5秒 or 1800文字でフラッシュ、コードブロックで送信
- Webhook URL は `/home/ubuntu/discord-bot/.env.journal-forward`
  （`JOURNAL_WEBHOOK_URL`、mode 600）
- **既知の落とし穴**: urllib のデフォルト User-Agent は Discord/Cloudflare の
  WAF に 403 で弾かれる。`User-Agent: kanae-journal-forward/1.0` を明示すること
- **残項目**: Worker 自体の死の検知は Cloudflare ダッシュボードの Workers アラート
  設定 or 外部 SaaS（UptimeRobot 等）を追加すること（監視の監視）

## Cloudflareオフロード実装状況（Phase 0-2, 2026-07-04）

計画: docs/2026-07-04-radical-cloudflare-outsourcing-plan.md

### 完了
- Phase 0 止血: mcp-brave-search / kanae-scheduler / mcp-chrome-devtools
  (一度も動いたことがないゾンビと判明) 停止、kanae_daily cron 削除、
  **backup_mem0.sh の毎日 bot 停止 cron 廃止** (R2 restore test 合格後)、
  MEM0_HISTORY_EMBED_ENABLED=0
- Phase 1: web_api/vscode_sync/music_analyzer/kanae-daily 削除 (/health は
  services/health_server.py へ移設)、Chroma ダイエット (41,493→15,021 vectors、
  recall 回帰 30/30 合格、cgroup 400M)、.state 2.4GB→299MB、死蔵22本掃除
- Phase 1.5: 共有 MCP 4本 (:8013 mem0 / :8014 second-opinion / :8015 oracle /
  :8016 x-scraper)。unit は infra/systemd/mcp-*-shared.service
- Phase 2: kanae-notify Worker (通知プロキシ)、kanae-vi-signal +
  kanae-intervention Worker (**シャドーモード DRY_RUN=1 で cron 稼働中**)

### vi/intervention の本番切替手順 (シャドー検証後)
1. 数日間、Worker の KV 記録 (`curl https://kanae-vi-signal.haroin57.workers.dev/` 等)
   とローカル scheduler の判定を突き合わせる
2. **vi-signal は切替前に要判断**: Worker 版はデータソースが日経VI→VIX に変わって
   いる (Workers から indexes.nikkei.co.jp が不安定なため)。閾値 >=30 の意味が
   変わるので、VIX 用に閾値を再調整するか日経VI の代替取得を実装するまで
   ローカル版を止めないこと
3. 切替: 通知先 webhook を作成 → `wrangler secret put WEBHOOK_VI_SIGNAL` (kanae-notify)
   → 各 Worker に `wrangler secret put NOTIFY_URL` (= https://kanae-notify.haroin57.workers.dev)
   → DRY_RUN=0 に → ローカル vi-signal / intervention-scheduler unit を disable
4. ロールバック: DRY_RUN=1 に戻してローカル unit を re-enable

### Browser Rendering 置換 (Phase 2-1) — 完了 (2026-07-04)
- table/math renderer は Cloudflare Browser Rendering REST (utils/cf_browser.py)。
  認証は .env の `CF_BROWSER_RENDERING_TOKEN`、アカウント ID は
  8574821c4ec0b24f61d59c5a15dc31f4 (**古い unit に埋まっていた fcf1f31d... は
  別アカウントの死んだ ID。使うと 401**)
- 実測: cache-miss 0.35〜1.61s (旧ローカル cold 9.1s)。KaTeX は jsDelivr CDN
- **free-tier は req/min が低い**: 描画連発で 429 → 1リトライ後 None (text fallback)。
  頻発するならリトライのバックオフ延長 or クライアント側キューを検討
- mermaid (mmdc) は別経路で**ローカル Chromium 依存のまま** — mermaid-cli と
  ~/.cache/puppeteer は削除禁止。トークン失効時も renderer は None を返して
  bot はテキスト劣化で継続する (落ちない)

### Workers 一覧 (ソース: ~/archives/cloudflare-workers-src-20260704/)
| Worker | 役割 | 状態 |
|---|---|---|
| kanae-health-probe | 外形監視 (毎分) + リソース閾値 | 本番 |
| kanae-mcp-brave | brave-search MCP (bot + Claude Code 共用) | 本番 |
| kanae-notify | 通知プロキシ (X-Notify-Auth) | 本番 (health target のみ配線) |
| kanae-vi-signal | 日次シグナル (0 9 * * 1-5 UTC) | シャドー |
| kanae-intervention | 介入スコア (*/30) | シャドー |
| kanae-egress-poc | egress 検証用 | 削除可 |

## systemd イベント配信（2026-07-04）

systemd のユニットライフサイクル（🟢 Started / ⚪ Stopped / 🔴 Failed・Main process
exited / 🟠 Scheduled restart）を haroin サーバーの systemctl ログチャンネル
（channel_id 1522737527771889824）へ配信する。

- デーモン: `scripts/systemd_forward.py`、unit `kanae-systemd-forward.service`
  （journal_forward.py の姉妹スクリプト。stdlib のみ）
- ソース: `journalctl -f -o json _PID=1`（システム journal の読み取りに
  `SupplementaryGroups=systemd-journal` が必要）
- ノイズ除去: sysstat / apt-daily / session scope 等は BLOCK 正規表現で落とす
  （実測 2h: 188行 → 36行）
- Webhook URL: `.env.systemd-forward`（SYSTEMD_WEBHOOK_URL、mode 600）

## 共有MCPログ配信（2026-07-04）

共有 MCP デーモン (mem0/second-opinion/oracle/x-scraper/papermcp) の WARNING 以上と
traceback を `#bot-mcp-log`（channel_id 1522739163412168885）へ配信する。

- デーモン: `scripts/mcp_log_forward.py`、unit `kanae-mcp-forward.service`
- ログ形式が unit ごとに違う（タイムスタンプ型 / LEVEL: 接頭辞型 / uvicorn access /
  supergateway 中継ダンプ）。**supergateway の JSON-RPC 中継行はスキーマ内に
  "error" 文字列を含むため無条件除外**してからマッチングする
- traceback はレベル接頭辞が無いので形状（Traceback / File " / XxxError:）で拾う
- 巨大行は300字で切る。Webhook URL は `.env.mcp-forward`（mode 600）

### 既知バグ: supergateway 切断時クラッシュ（パッチ済み 2026-07-04）
`mcp-oracle-shared` / `mcp-papermcp` が使う supergateway 3.4.3 は、クライアント切断後に
子プロセスが応答を返すと async な `transport.send()` の rejection を捕捉できず
`Error: No connection established for request ID: N` で **node ごと死ぬ**（今日 2 回発生）。
`Restart=always` で復帰するが in-flight ジョブが孤児化する。両 gateway に rejection を
`.catch()` するローカルパッチを適用し、`scripts/supergateway_apply_patches.sh` で自己修復
（oracle パッチと同じ機構、launcher が毎回再適用）。詳細は `docs/oracle_mcp.md` 追補3。
なお oracle の search デフォルト off は `~/.oracle/config.json {"search":"off"}` が正本
（env だけでは MCP 経路で復活する。同 doc 参照）。

## dev環境 + CI/CD (2026-07-04)

### 構成
- **本番**: /home/ubuntu/discord-bot (discord-bot.service, :3002, master ブランチ)
- **開発**: /home/ubuntu/discord-bot-dev (discord-bot-dev.service, :3003, dev ブランチ、
  bot は かなえdev id=1522741627662237866、mem0 は kanae_memories_dev コレクションで分離)

### CI (.github/workflows/test.yml)
- push (master/dev/feature/**) と PR で pytest 全 suite を GitHub ランナーで実行 (-n auto)
- ダミー API キー env で全緑。GitHub Secrets 不要

### 自動デプロイ (pull 型, scripts/auto_deploy.sh)
- systemd timer (discord-bot-autodeploy.timer / -dev-autodeploy.timer, 5分毎)
- `flock`取得とdirty-worktree・rollback pin検査後にoriginをfetchする。
- **CI (test.yml) が該当 SHA で conclusion=success の時のみ** ff pull対象とする。
- devは新しいtreeでsmokeを実行し、`/ready`成功時だけ
  `/var/lib/discord-bot-deploy/dev/successful.sha`を原子的に更新する。
- prodはdev receiptのSHAが対象commitと完全一致し、dev `/ready`のJSONが
  `status=ok`であることを要求する。
- prodは状態snapshotと復元リハーサル後にrestartし、5秒間隔・既定36回（180秒）の`/ready`失敗で自動rollbackする。
- rollbackはtag、期待旧SHA、失敗SHAを外部pinへ固定し、以後のauto deployを停止する。
- rollback runnerとprotected manifestはcheckout外のroot-owned trust rootを使用する。
- botとdeployは別UIDで動作し、bot unitはsourceと`.git`へ書き込めない。

### ブランチ保護 (pre-push hook)
- **master / dev への直接 push はローカル hook がブロック**する。必ず feature ブランチ → PR 経由
- dev を master に追随させる初期化のみ、gh api での ref 更新を使った (レビュー済み内容の ff)

### 開発フロー
1. feature ブランチで作業 → push → PR (base=dev)
2. CI green → dev にマージ → dev push で CI 再走 → auto_deploy が dev bot を更新
3. dev で検証 OK → dev を base に master へ PR → マージ → auto_deploy が本番を更新

### 既知の運用メモ
- dev bot の restart は kanae-systemd-forward に流れる (unit 名で判別可能。ノイズが増えたら
  systemd_forward.py の BLOCK に discord-bot-dev を追加)
- requirements.txt に未 pin の runtime 依存が ~23 個ある (dev クローン時に手動補完が必要)。
  別途 pin する価値あり

## 動的ツールループの予算 (ADR-0036)

ツールループは回数上限 (`ADMIN_MAX_TURNS`, 既定 400 に格上げ) を「非常停止」に
格下げし、実務上のリミッタを **リクエスト予算 + ループ検知** に移した
(`services/claude_api/loop_guard.py`)。上限・予算・ループ検知のいずれで抜けても
**作業は破棄されず** graceful checkpoint (本物の session + 進捗要約) として保存され、
「続けて」で full context から再開できる。`AgentLoopResult.paused_reason` に離脱理由
(`turn_ceiling` / `wallclock` / `output_tokens` / `tool_loop`) が入る。

チューニング用 env (全て `config/__init__.py`、既定で運用可):
- ウォールクロック予算(秒): `REQUEST_WALLCLOCK_BUDGET_S`=1800 (admin) /
  `RESTRICTED_WALLCLOCK_BUDGET_S`=120 / `SUBAGENT_WALLCLOCK_BUDGET_S`=600
- 累積出力トークン予算: `REQUEST_OUTPUT_TOKEN_BUDGET`=800000 /
  `RESTRICTED_OUTPUT_TOKEN_BUDGET`=40000 (footprint 上限。OAuth 枠なのでドル予算ではない)
- soft 閾値比: `REQUEST_BUDGET_SOFT_RATIO`=0.8 (到達で「まとめて停止」nudge を1回)
- ループ検知: `TOOL_LOOP_WINDOW`=12 / `TOOL_LOOP_REPEAT_WARN`=3 (nudge) /
  `TOOL_LOOP_REPEAT_BREAK`=5 (checkpoint) / `TOOL_LOOP_READONLY_SLACK`=3
  (Read/Glob/Grep の反復許容上乗せ)
- ログ: 予算 hard / ループ break は WARNING で journal に出る (#bot-journal-log に流れる)。
  頻発したら該当予算を上げるかループ検知閾値を見直す

# discord-bot Operations Runbook

## Deploy

```bash
cd /home/ubuntu/discord-bot
./scripts/bot-deploy.sh
```

The script:
1. Tags current HEAD as `pre-deploy-YYYYMMDD-HHMMSS` (rollback anchor)
2. Fast-forwards to `origin/master`
3. Reinstalls deps if `requirements.lock` changed
4. Restarts `discord-bot.service` via systemd
5. Polls `GET /health` up to 6 times (30 s total) — exits 0 on success, 3 on failure

## Rollback

```bash
./scripts/bot-rollback.sh pre-deploy-20260513-120000
```

Lists available tags if no argument is given. HEAD ends in detached state — investigate the bad commit before re-attaching to master.

## Health Check

```bash
curl 127.0.0.1:3002/health | jq
```

Expected: `{"status": "ok", ...}` with HTTP 200. Any non-200 or connection refused means the bot is down.

## Logs

```bash
journalctl -fu discord-bot          # live stream
journalctl -u discord-bot -n 200    # last 200 lines
```

## Disaster Recovery

Backups are written to `~/.local/state/discord-bot/backups/`.

To restore:
1. Stop the service: `sudo systemctl stop discord-bot`
2. Identify the latest backup: `ls -lt ~/.local/state/discord-bot/backups/ | head`
3. Copy the backup files back to their expected paths (sessions JSON, Mem0 DB, etc.)
4. Start the service: `sudo systemctl start discord-bot`
5. Confirm health: `curl 127.0.0.1:3002/health | jq`

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
# 1. 整合スナップショット（無停止・SQLite .backup API）+ 復元リハーサル
scripts/snapshot_state.sh                     # → ~/.local/state/discord-bot/refactor-snapshots/<ts>
scripts/snapshot_state.sh --verify <snap-dir> # integrity_check まで通ることを確認

# 2. 再起動前スモーク（red なら再起動しない）
scripts/smoke_bot.sh

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
- 通知: Discord Webhook → #kanae-monitoring（「おはなし」サーバー）。DOWN 継続中は
  30分毎に再通知
- 状態確認: https://kanae-health-probe.haroin57.workers.dev/ （現在の KV 状態を返す）
- ソース: /tmp/kanae-health-probe（wrangler プロジェクト。恒久保管先は要検討）
- **残項目**: Worker 自体の死の検知は Cloudflare ダッシュボードの Workers アラート
  設定 or 外部 SaaS（UptimeRobot 等）を追加すること（監視の監視）

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

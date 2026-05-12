# Security Handover — 2026-05-13

## C1: Token rotation (CRITICAL)
A bak file `scripts/toeic-reminder.sh.bak.20260508` was found potentially containing a live DISCORD_TOKEN.
Steps:
1. Discord Developer Portal → Bot → Reset Token
2. Copy new token into `/home/ubuntu/discord-bot/.env` as `DISCORD_TOKEN=<new>`
3. `systemctl restart discord-bot`
4. Confirm old token is revoked (bot reconnects with new token in logs)

## C2: Secure .env permissions
```bash
chmod 600 /home/ubuntu/discord-bot/.env
```
Verify: `stat -c %a /home/ubuntu/discord-bot/.env` → `600`

## M12: STATE_DIR permission check
```bash
chmod 700 /home/ubuntu/discord-bot/.state
chmod 700 /home/ubuntu/.local/state/discord-bot
```

## H11/H12: Install deploy/ assets
See `deploy/README.md` for full instructions.

Quick path:
```bash
sudo cp deploy/systemd/discord-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now discord-bot
crontab -e  # add: 0 9 * * * /home/ubuntu/discord-bot/deploy/cron/run-iran-monitor.sh
```

## Session migration note (H1 from session.py agent)
The session key schema changed to composite keys (channel_id + user_id).
Existing `sessions.json` is incompatible. On deploy:
```bash
mv /home/ubuntu/discord-bot/.state/sessions.json /home/ubuntu/discord-bot/.state/sessions.json.pre-h1-migration
systemctl restart discord-bot
```
Sessions will rebuild from Discord history on next use.

# Deploy assets

## Initial install
1. Copy `systemd/discord-bot.service` to `/etc/systemd/system/`.
2. `chmod 600 /home/ubuntu/discord-bot/.env` (C2 from audit)
3. `chmod 700 /home/ubuntu/discord-bot/.state /home/ubuntu/.local/state/discord-bot`
4. `systemctl daemon-reload && systemctl enable --now discord-bot`
5. Install cron: `crontab -e` → add `0 9 * * * /home/ubuntu/discord-bot/deploy/cron/run-iran-monitor.sh`

## Verification
- `systemd-analyze security discord-bot.service` should report Exposure level: MEDIUM or better
- `stat -c %a /home/ubuntu/discord-bot/.env` should output `600`
- `journalctl -fu discord-bot` shows no startup errors

## C1 — token rotation reminder
If you found a leaked token in `scripts/*.bak.*`:
1. Discord Developer Portal → Reset Bot Token
2. Update `DISCORD_TOKEN=` in `.env`
3. `systemctl restart discord-bot`

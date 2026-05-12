#!/usr/bin/env bash
set -euo pipefail
umask 077
LOG_DIR="${HOME}/.local/state/discord-bot"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"
exec /home/ubuntu/discord-bot/scripts/iran-monitor.sh >> "$LOG_DIR/iran-monitor.log" 2>&1

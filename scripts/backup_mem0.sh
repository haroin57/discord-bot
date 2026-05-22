#!/usr/bin/env bash
# Daily backup of the ChromaDB state directory.
# Requirements §3.4.3: stop chromadb-server → rsync → start, 7-day retention.
#
# Installed via /etc/cron.d/mem0-backup (root cron). Stop/start uses systemctl
# without sudo because the cron job itself runs as root.
#
# 2026-05-16: discord-bot is now stopped before chromadb and started after,
# inside the same window. Previously chromadb-only stop killed the bot via
# the now-removed Requires= dependency; the bot survives Wants= today, but
# we still want it down during the snapshot so it does not issue Mem0 calls
# against a dead backend.

set -euo pipefail

DB_PATH="/home/ubuntu/discord-bot/.state/mem0_db"
STATE_DIR="/home/ubuntu/discord-bot/.state"
DATE="$(date +%Y-%m-%d)"
BAK_DIR="${STATE_DIR}/mem0_db.bak.${DATE}"
RETENTION_DAYS=7
LOG_TAG="mem0_backup"

log() {
    logger -t "$LOG_TAG" "$1"
    echo "[$(date -Iseconds)] $1"
}

if [[ ! -d "$DB_PATH" ]]; then
    log "WARN db path missing: ${DB_PATH} — skipping backup"
    exit 0
fi

log "Stopping discord-bot.service before chromadb snapshot"
systemctl stop discord-bot.service

log "Stopping chromadb-server.service for consistent snapshot"
systemctl stop chromadb-server.service

# trap to always restart both services even if rsync fails — losing the
# backup is recoverable; leaving chromadb or the bot stopped is not.
# Order matters: start chromadb first so the bot's After=/Wants= ordering
# sees a healthy backend on boot.
restart_services() {
    log "Starting chromadb-server.service"
    systemctl start chromadb-server.service
    log "Starting discord-bot.service"
    systemctl start discord-bot.service
}
trap restart_services EXIT

log "rsync ${DB_PATH} -> ${BAK_DIR}"
rsync -a --delete "${DB_PATH}/" "${BAK_DIR}/"

log "Pruning backups older than ${RETENTION_DAYS} days"
find "$STATE_DIR" -maxdepth 1 -type d -name 'mem0_db.bak.*' -mtime "+${RETENTION_DAYS}" -print -exec rm -rf {} +

log "Backup complete: ${BAK_DIR}"

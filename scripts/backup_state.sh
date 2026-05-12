#!/usr/bin/env bash
# SRE-C4: weekly backup of bot state.
# Backs up .state/ + ChromaDB to ~/.local/state/discord-bot/backups/<date>/
# Rotates: keeps last 4 weekly snapshots.
set -euo pipefail
umask 077

BOT_DIR="/home/ubuntu/discord-bot"
STATE_DIR="${BOT_DIR}/.state"
# ChromaDB persistence path — adjust if mem0 stores elsewhere
CHROMA_DIR="${HOME}/.local/share/mem0"
BACKUP_ROOT="${HOME}/.local/state/discord-bot/backups"
DATE="$(date +%F)"
DEST="${BACKUP_ROOT}/${DATE}"

mkdir -p "${BACKUP_ROOT}"
chmod 700 "${BACKUP_ROOT}"

# M-2: prevent two concurrent cron-fired instances from racing on the
# rotation tail-rm or rsync target. flock -n exits non-zero immediately
# if another instance already holds the lock; we treat that as a clean
# no-op so cron doesn't keep firing failure mail.
LOCK_FILE="${BACKUP_ROOT}/backup.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[$(date -Iseconds)] another backup is running; exiting"
    exit 0
fi

mkdir -p "${DEST}"
chmod 700 "${DEST}"

LOG="${BACKUP_ROOT}/backup.log"
exec >>"${LOG}" 2>&1

echo "[$(date -Iseconds)] backup start -> ${DEST}"

if [[ -d "${STATE_DIR}" ]]; then
    rsync -a --delete "${STATE_DIR}/" "${DEST}/state/"
    echo "[$(date -Iseconds)] copied .state/ ($(du -sh "${DEST}/state" | cut -f1))"
else
    echo "[$(date -Iseconds)] WARNING: state dir missing: ${STATE_DIR}"
fi

if [[ -d "${CHROMA_DIR}" ]]; then
    rsync -a --delete "${CHROMA_DIR}/" "${DEST}/mem0/"
    echo "[$(date -Iseconds)] copied chromadb ($(du -sh "${DEST}/mem0" | cut -f1))"
else
    echo "[$(date -Iseconds)] WARNING: chromadb dir missing: ${CHROMA_DIR}"
fi

# Rotate: keep last 4 dated snapshots
cd "${BACKUP_ROOT}"
ls -1dt 20*/ 2>/dev/null | tail -n +5 | xargs -r rm -rf

echo "[$(date -Iseconds)] backup complete; current snapshots: $(ls -1d 20*/ 2>/dev/null | wc -l)"

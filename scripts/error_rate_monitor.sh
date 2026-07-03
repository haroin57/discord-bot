#!/usr/bin/env bash
# P0.25-d (docs/2026-07-03-grand-refactor-plan.md): error-rate gate for the
# refactor train. Human 24h-observation misses slow degradation; this compares
# the ERROR rate of the recent window against an absolute threshold and exits
# non-zero on breach so cron/CI can alert.
#
# Usage: scripts/error_rate_monitor.sh [window_minutes] [max_errors]
#   defaults: 15-minute window, >5 ERROR lines = breach
# Cron example (during observation periods after risky PRs):
#   */15 * * * * /home/ubuntu/discord-bot/scripts/error_rate_monitor.sh || \
#     echo "discord-bot error-rate breach $(date)" >> /tmp/bot_alerts.log
set -euo pipefail

BOT_DIR="/home/ubuntu/discord-bot"
LOG="${BOT_DIR}/bot.log"
WINDOW_MIN="${1:-15}"
MAX_ERRORS="${2:-5}"

[[ -f "${LOG}" ]] || { echo "[monitor] no ${LOG}; is the bot running here?"; exit 2; }

SINCE="$(date -d "-${WINDOW_MIN} minutes" '+%Y-%m-%d %H:%M')"
# Log lines start with "YYYY-MM-DD HH:MM:SS,ms LEVEL ..." — lexicographic
# compare on the timestamp prefix is safe.
ERRORS="$(awk -v since="${SINCE}" 'substr($0,1,16) >= since && $3 == "ERROR"' "${LOG}" | wc -l)"
WARNINGS="$(awk -v since="${SINCE}" 'substr($0,1,16) >= since && $3 == "WARNING"' "${LOG}" | wc -l)"

echo "[monitor] last ${WINDOW_MIN}m: ERROR=${ERRORS} WARNING=${WARNINGS} (threshold ${MAX_ERRORS})"
if [[ "${ERRORS}" -gt "${MAX_ERRORS}" ]]; then
    echo "[monitor] BREACH — recent errors:"
    awk -v since="${SINCE}" 'substr($0,1,16) >= since && $3 == "ERROR"' "${LOG}" | tail -5
    echo "[monitor] rollback guidance: scripts/bot-rollback.sh <tag> (see deploy/RUNBOOK.md)"
    exit 1
fi

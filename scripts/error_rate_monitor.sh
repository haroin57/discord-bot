#!/usr/bin/env bash
# P0.25-d (docs/2026-07-03-grand-refactor-plan.md): error-rate gate for the
# refactor train. Human 24h-observation misses slow degradation; this compares
# the ERROR rate of the recent window against an absolute threshold and exits
# non-zero on breach so cron/CI can alert.
#
# Reads journald, NOT bot.log: install_structured_logging() replaces every
# root handler with a JSON StreamHandler → journald, so the production
# process never writes bot.log at all (discovered 2026-07-03 — the first
# version of this monitor watched a file that only ever received leaked
# pytest output and reported ERROR=0 forever).
#
# Usage: scripts/error_rate_monitor.sh [window_minutes] [max_errors]
#   defaults: 15-minute window, >5 ERROR lines = breach
# Cron example (during observation periods after risky PRs):
#   */15 * * * * /home/ubuntu/discord-bot/scripts/error_rate_monitor.sh || \
#     echo "discord-bot error-rate breach $(date)" >> /tmp/bot_alerts.log
set -euo pipefail

WINDOW_MIN="${1:-15}"
MAX_ERRORS="${2:-5}"
UNIT="discord-bot"

if ! systemctl is-active --quiet "${UNIT}"; then
    echo "[monitor] ${UNIT} is not active"
    exit 2
fi

_lines() {
    journalctl -u "${UNIT}" --since "-${WINDOW_MIN} min" -o cat --no-pager 2>/dev/null
}

# Structured lines are JSON with a "level" field; count exact matches so a
# message merely containing the word ERROR doesn't inflate the rate.
ERRORS="$(_lines | grep -c '"level": "ERROR"' || true)"
WARNINGS="$(_lines | grep -c '"level": "WARNING"' || true)"

echo "[monitor] last ${WINDOW_MIN}m: ERROR=${ERRORS} WARNING=${WARNINGS} (threshold ${MAX_ERRORS})"
if [[ "${ERRORS}" -gt "${MAX_ERRORS}" ]]; then
    echo "[monitor] BREACH — recent errors:"
    _lines | grep '"level": "ERROR"' | tail -5
    echo "[monitor] rollback guidance: scripts/bot-rollback.sh <tag> (see deploy/RUNBOOK.md)"
    exit 1
fi

#!/bin/bash
# Monthly Kanae misattribution audit (dry-run report only).
#
# Cron entry:
#   0 9 1 * * /home/ubuntu/discord-bot/scripts/monthly_kanae_audit.sh
#
# Stops the bot briefly (~5 min) to free the Discord token, runs
# scripts/audit_kanae_facts.py in dry-run mode, writes a dated report
# under /tmp/, then restarts the bot.
#
# Deletions are NOT performed automatically. Review the report and run
# `scripts/validate_kanae_audit.py --commit` manually when satisfied.

set -uo pipefail

cd /home/ubuntu/discord-bot

DATE=$(date +%Y%m%d)
REPORT=/tmp/kanae_audit_${DATE}.md
CRONLOG=/tmp/kanae_audit_cron.log

echo "[$(date -Iseconds)] === monthly kanae audit start ===" >> "$CRONLOG"

# 1. Stop bot so the audit can claim the Discord token.
sudo /bin/systemctl stop discord-bot >> "$CRONLOG" 2>&1
sleep 3

# 2. Run the audit (dry-run by default; no --commit).
.venv/bin/python scripts/audit_kanae_facts.py \
  --max-concurrent 16 \
  --output "$REPORT" >> "$CRONLOG" 2>&1
status=$?
echo "[$(date -Iseconds)] audit exit=$status report=$REPORT" >> "$CRONLOG"

# 3. Always restart the bot, even on audit failure.
sudo /bin/systemctl start discord-bot >> "$CRONLOG" 2>&1

echo "[$(date -Iseconds)] === monthly kanae audit done ===" >> "$CRONLOG"
exit $status

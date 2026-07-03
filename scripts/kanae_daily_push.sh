#!/usr/bin/env bash
# kanae_daily_push.sh — Push kanae_daily.json to Workers KV
# KV namespace: KANAE_DAILY_KV (id: a57f7b7f9c754e41a5efca0d314fa8a4)
set -euo pipefail

WRANGLER="${WRANGLER:-$HOME/.volta/bin/wrangler}"
NAMESPACE_ID="a57f7b7f9c754e41a5efca0d314fa8a4"
STATE_FILE="/home/ubuntu/discord-bot/.state/kanae_daily.json"
MTIME=$(stat -c '%Y' "$STATE_FILE" 2>/dev/null || date +%s)
MTIME_ISO=$(date -u -d "@${MTIME}" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "[kanae_daily_push] Pushing $STATE_FILE → KV daily.json (mtime=${MTIME_ISO})"

"$WRANGLER" kv key put \
  --namespace-id "$NAMESPACE_ID" \
  --remote \
  --metadata "{\"mtime\":\"${MTIME_ISO}\"}" \
  "daily.json" \
  --path "$STATE_FILE"

echo "[kanae_daily_push] Done."

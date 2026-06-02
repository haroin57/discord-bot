#!/usr/bin/env bash
# OnFailure notifier for refresh-cli-identity.service (incident ③, 2026-06-02).
#
# A failed CLI-identity refresh used to be silent: the service just went to
# `failed` and nobody noticed until the identity drifted and API auth degraded.
# This script is wired via an OnFailure= drop-in so every failure surfaces.
# It posts to Discord when DISCORD_TOKEN (+ a channel) are available, and ALWAYS
# logs to the journal so the signal is never lost even if the POST fails.
#
# Never abort the OnFailure chain: always exit 0.
set -uo pipefail

UNIT="refresh-cli-identity.service"
# Default to haroin's bot-ops/debug channel; override via .env if desired.
CHANNEL_ID="${REFRESH_CLI_FAILURE_CHANNEL_ID:-1488369795807055964}"
TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
TAIL="$(journalctl -u "$UNIT" -n 12 --no-pager 2>/dev/null | tail -12)"

# Always log loudly to the journal (survives even if Discord is unreachable).
logger -t refresh-cli-failure "refresh-cli-identity FAILED at ${TS}"

MSG="⚠️ refresh-cli-identity.service が失敗しました (${TS})。CLI identity が古いまま放置されると将来 API 認証が劣化します。直近ログ:
\`\`\`
${TAIL}
\`\`\`"

if [[ -n "${DISCORD_TOKEN:-}" ]]; then
    # Build the JSON body with python3 (always present) so journal text with
    # quotes/newlines/backticks can't break the payload. Discord caps a single
    # message at 2000 chars — trim defensively.
    PAYLOAD="$(MSG="$MSG" /usr/bin/python3 -c \
        'import json,os;print(json.dumps({"content":os.environ["MSG"][:1900]}))')"
    if curl -sS -X POST \
        -H "Authorization: Bot ${DISCORD_TOKEN}" \
        -H "Content-Type: application/json" \
        "https://discord.com/api/v10/channels/${CHANNEL_ID}/messages" \
        --data "$PAYLOAD" >/dev/null 2>&1; then
        logger -t refresh-cli-failure "Discord alert sent to ${CHANNEL_ID}"
    else
        logger -t refresh-cli-failure "Discord alert POST failed (journal-only)"
    fi
else
    logger -t refresh-cli-failure "DISCORD_TOKEN unset; journal-only alert"
fi

exit 0

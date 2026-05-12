#!/bin/bash
# Iran ceasefire / Strait of Hormuz monitoring — runs via crontab every 6 hours
# Calls Claude CLI for analysis, sends result to Discord channel

set -euo pipefail

# Load .env if present (DISCORD_TOKEN, etc.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/../.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "${SCRIPT_DIR}/../.env"
    set +a
fi

CHANNEL_ID="1488369795807055964"
BOT_TOKEN="${DISCORD_TOKEN:?DISCORD_TOKEN environment variable is required}"
NOTIFY_USER="<@1037285234200297522>"  # b0nchichi (お父さん)
LOG="/tmp/iran-monitor.log"
LOCK="/tmp/iran-monitor.lock"

# Prevent overlapping runs
if [ -f "$LOCK" ]; then
    pid=$(cat "$LOCK" 2>/dev/null)
    if kill -0 "$pid" 2>/dev/null; then
        echo "$(date): Already running (pid=$pid), skipping" >> "$LOG"
        exit 0
    fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

echo "$(date): Starting Iran monitor run" >> "$LOG"

PROMPT="以下の情報を調べて、簡潔な日本語レポート（Discord投稿用、2000文字以内）を作成してください。

## 調査項目
1. イラン・米国間の停戦交渉の最新状況
2. ホルムズ海峡の航行リスク・軍事的動向
3. イラン関連の制裁・外交の動き
4. 原油市場への影響シグナル

## 出力形式
- 🔍 **イラン情勢定期レポート** というヘッダーで始める
- 各項目を箇条書きで簡潔に
- 重要度が高い変化があれば ⚠️ マークをつける
- 特に変化がなければ「大きな変動なし」と報告
- 最後に情報ソース（URL）を1-2個つける"

# Run Claude CLI with tools (brave-search + x-scraper for comprehensive coverage)
RESULT=$(cd /home/ubuntu && claude -p "$PROMPT" --max-turns 15 2>>"$LOG") || {
    echo "$(date): Claude CLI failed" >> "$LOG"
    RESULT="❌ Claude CLI実行エラー。手動で確認してください。"
}

# Truncate to Discord's 2000 char limit
if [ ${#RESULT} -gt 1950 ]; then
    RESULT="${RESULT:0:1950}..."
fi

# Prepend mention
MESSAGE="${NOTIFY_USER}
${RESULT}"

# Send to Discord
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "https://discord.com/api/v10/channels/${CHANNEL_ID}/messages" \
    -H "Authorization: Bot ${BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg content "$MESSAGE" '{content: $content}')")

echo "$(date): Sent to Discord (HTTP $HTTP_CODE)" >> "$LOG"

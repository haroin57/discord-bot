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
if [[ -z "${BOT_TOKEN// /}" ]]; then
    echo "ERROR: DISCORD_TOKEN is whitespace-only" >&2
    exit 1
fi
NOTIFY_USER="<@1037285234200297522>"  # b0nchichi (お父さん)

# State directory (XDG-style) — owned by the running user, mode 700 on create
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/discord-bot"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true
LOG="$STATE_DIR/iran-monitor.log"
LOCK="$STATE_DIR/iran-monitor.lock"

# Prevent overlapping runs via flock (atomic; symlink-attack proof).
# fd 200 stays open for the lifetime of the script; the lock releases on exit.
# Pre-create the lockfile with mode 0o600 for consistency with other state files.
[[ -e "$LOCK" ]] || install -m 600 /dev/null "$LOCK"
exec 200>"$LOCK"
if ! flock -n 200; then
    echo "$(date): Already running, skipping" >> "$LOG"
    exit 0
fi

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
RESULT=$(cd /home/ubuntu && claude -p "$PROMPT" --max-turns 15 --allowed-tools "WebFetch,WebSearch" 2>>"$LOG") || {
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

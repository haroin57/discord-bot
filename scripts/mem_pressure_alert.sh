#!/usr/bin/env bash
# mem_pressure_alert.sh — 初回 OOM の前に気づくための軽量メモリ逼迫アラート。
#
# PSI(memory some avg60 = 直近60秒でメモリ待ちにより stall した時間割合) と
# MemAvailable を監視し、閾値超過で既存 ops webhook(.env.systemd-forward) に通知。
# cron 5分毎想定。cooldown でスパム抑制。--test で即送信して疎通確認。
#
# 設計意図: OOM は37日間ゼロだが「初回」を事後に知るのでは遅い。PSI は実メモリ
# 逼迫を RSS 合計より正確に捉える（cgroup reclaim/swap-in の痛みを直接見る）。
set -uo pipefail

PSI_WARN="${MEM_ALERT_PSI:-8}"                 # some avg60 %
AVAIL_WARN_MB="${MEM_ALERT_AVAIL_MB:-600}"     # MemAvailable 下限 MB
COOLDOWN_S="${MEM_ALERT_COOLDOWN_S:-1800}"     # 30分
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/discord-bot/mem_alert.last"
ENV_FILE="/home/ubuntu/discord-bot/.env.systemd-forward"

sanitize() { tr -d '"\\' | tr '\n' ' '; }  # JSON 破壊文字を除去

WEBHOOK=""
# 変数名は SYSTEMD_WEBHOOK_URL 等。末尾が WEBHOOK_URL= の行を拾う。
[ -f "$ENV_FILE" ] && WEBHOOK=$(grep -E 'WEBHOOK_URL=' "$ENV_FILE" | head -1 \
    | sed -E "s/^[^=]*=//; s/^[\"']//; s/[\"']$//; s/[[:space:]]//g")
[ -z "$WEBHOOK" ] && { echo "no webhook url" >&2; exit 0; }

send() {
    local content; content=$(printf '%s' "$1" | sanitize)
    curl -s -m 10 -H 'Content-Type: application/json' \
         --data "{\"content\": \"${content}\"}" "$WEBHOOK" >/dev/null 2>&1
}

host=$(hostname)
top3=$(ps -eo rss,comm --sort=-rss --no-headers 2>/dev/null | head -3 \
        | awk '{printf "%s(%dMB) ",$2,$1/1024}' | sanitize)

if [ "${1:-}" = "--test" ]; then
    send "✅ mem_pressure_alert 疎通テスト ($host) — top: $top3"
    echo "test alert sent"; exit 0
fi

psi=$(awk '/^some/{for(i=1;i<=NF;i++) if($i ~ /^avg60=/){sub("avg60=","",$i); print $i}}' /proc/pressure/memory 2>/dev/null)
[ -z "$psi" ] && psi=0
avail_mb=$(awk '/^MemAvailable/{print int($2/1024)}' /proc/meminfo)
swap_used_mb=$(awk '/^SwapTotal/{t=$2}/^SwapFree/{f=$2}END{print int((t-f)/1024)}' /proc/meminfo)

reasons=""
awk "BEGIN{exit !($psi+0 >= $PSI_WARN)}" && reasons="${reasons}PSI(mem some avg60)=${psi}%(>=${PSI_WARN}) "
[ "${avail_mb:-99999}" -lt "$AVAIL_WARN_MB" ] && reasons="${reasons}MemAvailable=${avail_mb}MB(<${AVAIL_WARN_MB}) "
[ -z "$reasons" ] && exit 0

# cooldown
now=$(date +%s)
if [ -f "$STATE" ]; then
    last=$(cat "$STATE" 2>/dev/null || echo 0)
    [ $((now - last)) -lt "$COOLDOWN_S" ] && exit 0
fi
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
echo "$now" > "$STATE"

send "⚠️ メモリ逼迫の兆候 ($host)\\n${reasons}\\nMemAvailable=${avail_mb}MB / Swap使用=${swap_used_mb}MB\\ntop: ${top3}"
exit 0

#!/usr/bin/env bash
# gpt-5.5-pro の回答をセッション非依存で回収し続ける常駐スクリプト。
# `setsid nohup` で起動され、Claude Code CLI(親)が切れても生き残る。
# gpt-5.5-pro の TPM 上限(50k)が厳しく reasoning 出力が大きいため、
# failed/overload の度に TPM ウィンドウを空けて次 slug へ submit しなおす。
# 回答が取れたら OUT に、完了状態を DONE マーカーに書いて exit。
#
# 起動: setsid nohup bash scripts/mem0_oracle_watch.sh >log 2>&1 </dev/null &
# 進捗: cat /tmp/discord_bot_attachments/mem0_oracle_watch.log
# 完了確認: cat /tmp/discord_bot_attachments/mem0_oracle.done
set -u
export PATH="$HOME/.volta/bin:$PATH"
cd /home/ubuntu/discord-bot || exit 1
export OPENAI_API_KEY=$(.venv/bin/python -c "import os;from dotenv import load_dotenv;load_dotenv('.env');print(os.environ.get('OPENAI_API_KEY',''))" 2>/dev/null)

OUT=/tmp/discord_bot_attachments/mem0_oracle_result_final.txt
DONE=/tmp/discord_bot_attachments/mem0_oracle.done
PROMPT=/tmp/discord_bot_attachments/mem0_oracle_prompt2.txt

rm -f "$DONE"
END=$(( $(date +%s) + 5400 ))   # 最大 90 分粘る
n=10                            # mem0-arch-review-10 から開始（既 submit 済み想定）

log() { echo "[$(date -Iseconds)] $*"; }

log "watch start (pid=$$, until $(date -d @${END} -Iseconds 2>/dev/null || echo +90m))"
while [ "$(date +%s)" -lt "$END" ]; do
  slug="mem0-arch-review-${n}"
  st=$(oracle session "$slug" 2>&1)
  if echo "$st" | grep -qE "^Answer:|· gpt-5.5-pro\[|Status: completed"; then
    echo "$st" > "$OUT"
    echo "SUCCESS slug=$slug at $(date -Iseconds)" > "$DONE"
    log "SUCCESS slug=$slug -> $OUT"
    exit 0
  fi
  if echo "$st" | grep -qE "Status: running|gpt-5.5-pro — (pending|running)"; then
    log "slug=$slug running, poll again in 30s"
    sleep 30
    continue
  fi
  # failed / overload / TPM / 未 submit → 次 slug で submit しなおし
  reason=$(echo "$st" | grep -iE "rate|overload|Limit|error|failed" | head -1)
  log "slug=$slug not-running (${reason:-unknown}). resubmit after TPM window."
  n=$(( n + 1 ))
  slug="mem0-arch-review-${n}"
  sleep 65                       # TPM ウィンドウを空ける
  oracle -m gpt-5.5-pro -s "$slug" -p "$(cat "$PROMPT")" >/dev/null 2>&1
  log "submitted slug=$slug, wait 80s for running-state"
  sleep 80
done
echo "TIMEOUT at $(date -Iseconds)" > "$DONE"
oracle session "mem0-arch-review-${n}" > "$OUT" 2>&1
log "TIMEOUT — gave up after 90m"

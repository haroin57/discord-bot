#!/usr/bin/env bash
# Switch the Codex model used by the prod bot (main Codex route + Mem0 aux LLM).
#
# Credentials env files are not touched. A later-sorting systemd drop-in adds
# one more EnvironmentFile; systemd applies EnvironmentFile entries in order,
# so this file's CODEX_MODEL / CODEX_APP_SERVER_MODEL win.
#
# Usage:   sudo bash scripts/switch_codex_model.sh [MODEL]      (default gpt-5.6-terra)
#          UNITS="discord-bot discord-bot-dev" sudo -E bash ...  (multiple units)
#          SKIP_PREFLIGHT=1 ...                                  (skip the live model check)
# Rollback: sudo bash scripts/switch_codex_model.sh --rollback
set -euo pipefail

MODEL="${1:-gpt-5.6-terra}"
UNITS="${UNITS:-discord-bot}"
ENV_FILE=/etc/discord-bot/release/codex-model.env
DROPIN_NAME=zzzz-codex-model.conf
BOT_USER=kanae-bot
BOT_HOME=/var/lib/kanae-bot

[[ $EUID -eq 0 ]] || { echo "root で実行してください: sudo bash $0 $*" >&2; exit 1; }

restart_and_wait() {
  local unit=$1
  systemctl restart "$unit"
  for _ in $(seq 1 30); do
    systemctl is-active --quiet "$unit" && return 0
    sleep 2
  done
  echo "!! $unit が active になりません: journalctl -u $unit -n 80 を確認してください" >&2
  return 1
}

if [[ "$MODEL" == "--rollback" ]]; then
  for unit in $UNITS; do
    rm -f "/etc/systemd/system/${unit}.service.d/${DROPIN_NAME}"
  done
  rm -f "$ENV_FILE"
  systemctl daemon-reload
  for unit in $UNITS; do restart_and_wait "$unit"; echo "rollback: $unit restarted"; done
  exit 0
fi

[[ "$MODEL" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "不正なモデル名: $MODEL" >&2; exit 1; }

# 1. Preflight: prove the model works for the bot's own ChatGPT account
#    before touching prod. A retired model fails here with a 400.
CODEX_BIN=$(systemctl show discord-bot -p Environment --value | tr ' ' '\n' | sed -n 's/^CODEX_BIN=//p')
CODEX_BIN=${CODEX_BIN:-/home/ubuntu/.volta/tools/image/packages/@openai/codex/bin/codex}
if [[ "${SKIP_PREFLIGHT:-0}" != 1 ]]; then
  echo "== preflight: $MODEL を $BOT_USER の Codex で呼び出し中"
  out=$(sudo -u "$BOT_USER" env HOME="$BOT_HOME" PATH=/usr/local/bin:/usr/bin:/bin \
        timeout 120 "$CODEX_BIN" exec --skip-git-repo-check -m "$MODEL" \
        -c model_reasoning_effort=low -C "$BOT_HOME/state/codex_auxiliary" \
        "Reply with OK only." 2>&1) || true
  if ! grep -qx 'OK' <<<"$out"; then
    echo "!! preflight 失敗。prod は変更していません。" >&2
    grep -E 'ERROR|error' <<<"$out" | tail -3 >&2
    exit 1
  fi
  echo "   OK"
fi

# 2. Env file (no secrets, model names only)
install -d -m 0755 "$(dirname "$ENV_FILE")"
[[ -f "$ENV_FILE" ]] && cp -a "$ENV_FILE" "${ENV_FILE}.bak-$(date +%Y%m%d%H%M%S)"
umask 022
cat >"$ENV_FILE" <<EOF
# Managed by scripts/switch_codex_model.sh ($(date -Iseconds))
CODEX_MODEL=$MODEL
CODEX_APP_SERVER_MODEL=$MODEL
EOF
echo "== wrote $ENV_FILE"

# 3. Drop-in per unit (sorts after zzz-mem0-alpha-performance.conf)
for unit in $UNITS; do
  dir="/etc/systemd/system/${unit}.service.d"
  install -d -m 0755 "$dir"
  printf '[Service]\nEnvironmentFile=%s\n' "$ENV_FILE" >"$dir/$DROPIN_NAME"
  echo "== wrote $dir/$DROPIN_NAME"
done
systemctl daemon-reload

# 4. Restart and verify the running process actually sees the new model
for unit in $UNITS; do
  echo "== restarting $unit"
  since=$(date '+%Y-%m-%d %H:%M:%S')
  restart_and_wait "$unit"
  # Type=simple reports "Started" at fork time; until exec the MainPID is
  # still systemd-executor and /proc/PID/environ lacks the unit env. Poll.
  seen=
  for _ in $(seq 1 15); do
    pid=$(systemctl show "$unit" -p MainPID --value)
    seen=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | grep -E '^CODEX(_APP_SERVER)?_MODEL=' || true)
    [[ -n "$seen" ]] && break
    sleep 2
  done
  echo "$seen" | sed 's/^/   /'
  if grep -qv "=$MODEL\$" <<<"$seen"; then
    echo "!! $unit のプロセス env が $MODEL になっていません（後勝ちの別 EnvironmentFile が無いか確認）" >&2
    exit 1
  fi

  echo "== $unit: Codex 呼び出しを最大 5 分観測（Ctrl-C で中断しても変更はそのまま残ります）"
  for _ in $(seq 1 30); do
    log=$(journalctl -u "$unit" --since "$since" --no-pager -o cat 2>/dev/null || true)
    ok=$(grep -c "Codex thread ready.*model=$MODEL" <<<"$log" || true)
    bad=$(grep -c 'BotOAuthBackend.complete failed' <<<"$log" || true)
    acked=$(grep -c 'Mem0 batch retained for retry' <<<"$log" || true)
    if (( ok > 0 || bad > 0 )); then break; fi
    sleep 10
  done
  echo "   thread_ready(model=$MODEL)=$ok  aux_failures=$bad  batch_retained=$acked"
  if (( bad > 0 )); then
    echo "   直近の失敗:"; grep 'BotOAuthBackend.complete failed' <<<"$log" | tail -2 | cut -c1-300
  fi
done
echo "完了。ロールバック: sudo bash $0 --rollback"

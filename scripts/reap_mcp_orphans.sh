#!/usr/bin/env bash
# reap_mcp_orphans.sh — ADR-0037 stateless-gateway leak の最終保険（reaper）。
#
# supergateway の in-process killTree（transport.onclose → process.kill(-pgid)）は
# 「クライアント切断」経路の孤児化を防ぐが、supergateway 本体がハードクラッシュ
# （OOM-kill / kill -9 / セグフォ）した場合は killTree を実行できず、稼働中の
# 子（oracle-mcp / papermcp backend）が PPid=1 に reparent して孤児化・積み上がる。
# この sweep はその取りこぼしだけを回収する。
#
# 安全原則: **PPid==1（＝親 supergateway が既に消滅）でパッケージ名が一致する
# プロセスのみ**を対象にする。稼働中の backend は必ず supergateway を親に持つ
# （PPid!=1）ので絶対に巻き込まない。加えて MIN_AGE 未満は「reparent 直後の
# レース」とみなしスキップ。--dry-run で対象列挙のみ。
set -uo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

LOG="${XDG_STATE_HOME:-$HOME/.local/state}/discord-bot/mcp_reaper.log"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

# backend の cmdline にのみ現れる一意な文字列（wrapper shell/launch.sh も同 pgid）
PATTERNS='@steipete/oracle|@telagod/papermcp'
# supergateway 本体は systemd サービスの main プロセスで、正当に PPid==1（init の
# 子）になる。かつ cmdline に "supergateway" を含む。これを reap すると生きている
# ゲートウェイを殺すので**必ず除外**する。復帰は systemd(Restart) の責務。
EXCLUDE='supergateway'
MIN_AGE=60   # 秒。reparent 直後の一過性 PPid==1 を避ける
SIGKILL_DELAY=15

now_iso() { date -Is; }
reaped=0
declare -A seen_pgid=()

# pid ppid pgid etimes(経過秒) args
while read -r pid ppid pgid etimes cmd; do
    [[ "$ppid" == "1" ]] || continue
    [[ "$etimes" =~ ^[0-9]+$ ]] || continue
    (( etimes >= MIN_AGE )) || continue
    grep -qE "$PATTERNS" <<<"$cmd" || continue
    grep -qE "$EXCLUDE" <<<"$cmd" && continue   # gateway 本体は絶対に触らない
    # 同一グループは1回だけ処理
    [[ -n "${seen_pgid[$pgid]:-}" ]] && continue
    seen_pgid[$pgid]=1

    if (( DRY_RUN )); then
        echo "[dry-run] would reap orphan pid=$pid pgid=$pgid age=${etimes}s cmd=${cmd:0:90}"
    else
        # グループごと TERM → 猶予後 KILL（supergateway 本体の killTree と同じ流儀）
        kill -TERM "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
        echo "$(now_iso) reaped orphan pid=$pid pgid=$pgid age=${etimes}s cmd=${cmd:0:90}" >> "$LOG"
        ( sleep "$SIGKILL_DELAY"; kill -KILL "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true ) &
    fi
    reaped=$((reaped + 1))
done < <(ps -eo pid=,ppid=,pgid=,etimes=,args=)

if (( DRY_RUN )); then
    echo "[dry-run] 対象孤児グループ数: $reaped"
elif (( reaped > 0 )); then
    echo "$(now_iso) reaper: killed $reaped orphan group(s)" >> "$LOG"
fi
exit 0

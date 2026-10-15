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

# --- headless Chrome orphans (2026-10-16 memory-diet) ---------------------
# puppeteer は Chrome を detached で起動し、Chrome は自力で
# app-org.chromium.Chromium-<pid>.scope へ移るため、親（render daemon /
# mmdc / テスト）が異常終了すると PPid==1 のまま誰にも回収されない（実測 7 個
# 1.6GB PSS）。render daemon は pipe 接続化で自己終了するようにしたが、旧コードや
# 手動実行の取りこぼしをここで回収する。対象は「PPid==1」かつ「puppeteer 既定
# プロファイルを使う top-level Chrome」かつ「CHROME_MIN_AGE 以上」のみ。
# 親が生きている Chrome（PPid!=1）は絶対に触らない。
CHROME_MIN_AGE="${CHROME_MIN_AGE:-300}"
CHROME_PROFILE_RE='--user-data-dir=/tmp/puppeteer_dev_chrome_profile-[A-Za-z0-9]+'
chrome_reaped=0
while read -r pid ppid etimes cmd; do
    [[ "$ppid" == "1" ]] || continue
    [[ "$etimes" =~ ^[0-9]+$ ]] || continue
    (( etimes >= CHROME_MIN_AGE )) || continue
    [[ "$cmd" == *chrome* ]] || continue
    [[ "$cmd" == *--type=* ]] && continue      # 子プロセスは本体と一緒に消える
    grep -qE -- "$CHROME_PROFILE_RE" <<<"$cmd" || continue
    if (( DRY_RUN )); then
        echo "[dry-run] would reap orphan chrome pid=$pid age=${etimes}s"
    else
        kill -TERM "$pid" 2>/dev/null || true
        echo "$(now_iso) reaped orphan chrome pid=$pid age=${etimes}s" >> "$LOG"
        ( sleep "$SIGKILL_DELAY"; kill -KILL "$pid" 2>/dev/null || true ) &
    fi
    chrome_reaped=$((chrome_reaped + 1))
done < <(ps -eo pid=,ppid=,etimes=,args=)

# 使用中でない古い puppeteer プロファイル（tmpfs = RAM）を削除する。
CHROME_PROFILE_MAX_AGE_MIN="${CHROME_PROFILE_MAX_AGE_MIN:-60}"
in_use=$(ps -eo args= | grep -oE -- "$CHROME_PROFILE_RE" | sed 's/^--user-data-dir=//' | sort -u)
profiles_removed=0
while IFS= read -r -d '' dir; do
    grep -qxF -- "$dir" <<<"$in_use" && continue
    if (( DRY_RUN )); then
        echo "[dry-run] would remove stale chrome profile $dir"
    else
        rm -rf --one-file-system -- "$dir" && profiles_removed=$((profiles_removed + 1))
    fi
done < <(find /tmp -maxdepth 1 -type d -name 'puppeteer_dev_chrome_profile-*' -user "$(id -u)" -mmin "+$CHROME_PROFILE_MAX_AGE_MIN" -print0 2>/dev/null)

if (( DRY_RUN )); then
    echo "[dry-run] 対象孤児グループ数: $reaped"
    echo "[dry-run] 対象孤児 Chrome 数: $chrome_reaped"
else
    (( reaped > 0 )) && echo "$(now_iso) reaper: killed $reaped orphan group(s)" >> "$LOG"
    (( chrome_reaped > 0 )) && echo "$(now_iso) reaper: killed $chrome_reaped orphan chrome(s)" >> "$LOG"
    (( profiles_removed > 0 )) && echo "$(now_iso) reaper: removed $profiles_removed stale chrome profile(s)" >> "$LOG"
fi
# 子 subshell（SIGKILL 猶予）を待たずに cron を終わらせる
exit 0

#!/usr/bin/env bash
# apply_memory_diet_host.sh — host-level part of the 2026-10-16 memory diet.
#
# Usage: sudo scripts/apply_memory_diet_host.sh [--dry-run] [--with-pnpm] [--restart-chroma]
#
#   1. Install the ChromaDB memory guard drop-in and the daily restart timer,
#      then apply the limits to the running unit without restarting it.
#   2. --restart-chroma: restart chromadb-server once to release the swap its
#      leak already holds. Mem0 searches return nothing for about 1-2 minutes.
#   3. Install the orphan reaper to /usr/local/libexec/kanae and run it from
#      a systemd timer every 10 minutes. The installed copy does not depend on
#      any git checkout being up to date. Then run it once now.
#   4. Install the Mermaid wrapper as /usr/local/bin/mmdc-safe.
#   5. Order mem0-hook-daemon after mcp-mem0-shared (the hook is now a proxy).
#   6. --with-pnpm: pin pnpm's store to the disk and delete /tmp/.pnpm-store.
#      pnpm puts its store on the project's filesystem, and /tmp is tmpfs (RAM).
#
# Without --restart-chroma the script is idempotent and does not disturb
# running services. Code changes (render daemon, hook proxy) ship through the
# normal PR/deploy path.
set -euo pipefail

DRY_RUN=0
WITH_PNPM=0
RESTART_CHROMA=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --with-pnpm) WITH_PNPM=1 ;;
        --restart-chroma) RESTART_CHROMA=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
owner="${SUDO_USER:-ubuntu}"
units=/etc/systemd/system
run() {
    if (( DRY_RUN )); then echo "[dry-run] $*"; else echo "+ $*"; "$@"; fi
}

if (( ! DRY_RUN )) && [[ $EUID -ne 0 ]]; then
    echo "run with sudo (or --dry-run)" >&2
    exit 1
fi

chroma_mem() {
    systemctl show chromadb-server -p MemoryCurrent -p MemorySwapCurrent -p MemoryHigh -p MemoryMax -p MemorySwapMax | sed 's/^/  /'
}

echo "== before"
chroma_mem
free -m | sed 's/^/  /'

echo "== 1. chroma memory guard"
run install -D -m 0644 "$repo/infra/systemd/chromadb-server.service.d/60-memory-guard.conf" \
    "$units/chromadb-server.service.d/60-memory-guard.conf"
run install -m 0644 "$repo/infra/systemd/chromadb-restart.service" "$units/chromadb-restart.service"
run install -m 0644 "$repo/infra/systemd/chromadb-restart.timer" "$units/chromadb-restart.timer"
run systemctl daemon-reload
run systemctl enable --now chromadb-restart.timer
# daemon-reload does not push new cgroup limits to a running unit.
guard=$(sed -n 's/^\(Memory[A-Za-z]*\)=\(.*\)$/\1=\2/p' "$repo/infra/systemd/chromadb-server.service.d/60-memory-guard.conf")
# shellcheck disable=SC2086
run systemctl set-property --runtime chromadb-server.service $guard

if (( RESTART_CHROMA )); then
    echo "== 2. restart chroma"
    run systemctl restart chromadb-server.service
    if (( ! DRY_RUN )); then
        for _ in $(seq 1 30); do
            curl -fsS -o /dev/null http://127.0.0.1:8765/api/v2/heartbeat && break
            sleep 1
        done
        curl -fsS http://127.0.0.1:8765/api/v2/heartbeat && echo "  chroma heartbeat ok"
    fi
fi

echo "== 3. orphan reaper (installed copy + timer)"
run install -D -m 0755 "$repo/scripts/reap_mcp_orphans.sh" /usr/local/libexec/kanae/reap_mcp_orphans.sh
run install -m 0644 "$repo/infra/systemd/kanae-orphan-reaper.service" "$units/kanae-orphan-reaper.service"
run install -m 0644 "$repo/infra/systemd/kanae-orphan-reaper.timer" "$units/kanae-orphan-reaper.timer"
run systemctl daemon-reload
run systemctl enable --now kanae-orphan-reaper.timer
if (( DRY_RUN )); then
    sudo -u "$owner" bash "$repo/scripts/reap_mcp_orphans.sh" --dry-run | grep -v 'stale chrome profile' || true
else
    run systemctl start kanae-orphan-reaper.service
fi

echo "== 4. mmdc-safe"
run install -m 0755 "$repo/scripts/mmdc_safe.sh" /usr/local/bin/mmdc-safe

echo "== 5. hook daemon ordering"
run install -D -m 0644 "$repo/infra/systemd/mem0-hook-daemon.service.d/20-after-mcp-mem0.conf" \
    "$units/mem0-hook-daemon.service.d/20-after-mcp-mem0.conf"
run systemctl daemon-reload

if (( WITH_PNPM )); then
    echo "== 6. move pnpm store off tmpfs"
    pnpm_bin="$(sudo -u "$owner" -i bash -lc 'command -v pnpm' || true)"
    if [[ -n "$pnpm_bin" ]]; then
        run sudo -u "$owner" -i "$pnpm_bin" config set store-dir "/home/$owner/.local/share/pnpm/store" --global
    fi
    # pnpm usually runs as a corepack/node shim, so its comm is "node": match
    # on the command line, and check that nothing holds the store open.
    if pgrep -f -- '(^|/)pnpm(\.c?js)?( |$)|corepack.*pnpm' >/dev/null \
        || { [[ -d /tmp/.pnpm-store ]] && fuser -sm /tmp/.pnpm-store 2>/dev/null; }; then
        echo "  pnpm is running or the store is in use; skipping /tmp/.pnpm-store removal"
    elif [[ -d /tmp/.pnpm-store ]]; then
        # Rename first so a pnpm started mid-delete creates a fresh store
        # instead of reading a half-deleted one.
        trash="/tmp/.pnpm-store.deleting.$$"
        run mv /tmp/.pnpm-store "$trash"
        run rm -rf --one-file-system "$trash"
    fi
fi

echo "== verify"
if (( ! DRY_RUN )); then
    want_max=$(sed -n 's/^MemoryMax=//p' "$repo/infra/systemd/chromadb-server.service.d/60-memory-guard.conf")
    got_max=$(systemctl show chromadb-server -p MemoryMax --value)
    want_bytes=$(numfmt --from=iec "$want_max")
    if [[ "$got_max" != "$want_bytes" ]]; then
        echo "  ERROR: chroma MemoryMax is $got_max, expected $want_bytes" >&2
        exit 1
    fi
    echo "  chroma MemoryMax ok ($want_max)"
    systemctl is-active kanae-orphan-reaper.timer chromadb-restart.timer | paste -sd' ' | sed 's/^/  timers: /'
fi

echo "== after"
chroma_mem
free -m | sed 's/^/  /'
df -h /tmp | sed 's/^/  /'

#!/usr/bin/env bash
# apply_memory_diet_host.sh — host-level part of the 2026-10-16 memory diet.
#
# Usage: sudo scripts/apply_memory_diet_host.sh [--dry-run] [--with-pnpm]
#
#   1. Install the ChromaDB memory guard drop-in and the daily restart timer.
#   2. Restart chromadb-server once to release the swap its leak holds (~3GB).
#      Mem0 searches fail for a few seconds and the bots degrade to "no memories".
#   3. Run the orphan reaper from this checkout (orphan Chrome + stale profiles).
#   4. --with-pnpm: pin pnpm's store to the disk and delete /tmp/.pnpm-store.
#      pnpm puts its store on the project's filesystem, and /tmp is tmpfs (RAM).
#
# The script is idempotent. Code changes (render daemon, hook proxy, mmdc
# wrapper) ship through the normal PR/deploy path; this script only touches
# the host.
set -euo pipefail

DRY_RUN=0
WITH_PNPM=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --with-pnpm) WITH_PNPM=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
owner="${SUDO_USER:-ubuntu}"
run() {
    if (( DRY_RUN )); then echo "[dry-run] $*"; else echo "+ $*"; "$@"; fi
}

if (( ! DRY_RUN )) && [[ $EUID -ne 0 ]]; then
    echo "run with sudo (or --dry-run)" >&2
    exit 1
fi

echo "== before"
systemctl show chromadb-server -p MemoryCurrent -p MemorySwapCurrent -p MemorySwapMax | sed 's/^/  /'
free -m | sed 's/^/  /'

echo "== 1. chroma memory guard"
run install -D -m 0644 "$repo/infra/systemd/chromadb-server.service.d/60-memory-guard.conf" \
    /etc/systemd/system/chromadb-server.service.d/60-memory-guard.conf
run install -m 0644 "$repo/infra/systemd/chromadb-restart.service" /etc/systemd/system/chromadb-restart.service
run install -m 0644 "$repo/infra/systemd/chromadb-restart.timer" /etc/systemd/system/chromadb-restart.timer
run systemctl daemon-reload
run systemctl enable --now chromadb-restart.timer

echo "== 2. restart chroma"
run systemctl restart chromadb-server.service
if (( ! DRY_RUN )); then
    for _ in $(seq 1 30); do
        curl -fsS -o /dev/null http://127.0.0.1:8765/api/v2/heartbeat && break
        sleep 1
    done
    curl -fsS http://127.0.0.1:8765/api/v2/heartbeat && echo "  chroma heartbeat ok"
fi

echo "== 3. reap orphan Chrome and stale profiles"
if (( DRY_RUN )); then
    sudo -u "$owner" bash "$repo/scripts/reap_mcp_orphans.sh" --dry-run | tail -3
else
    sudo -u "$owner" bash "$repo/scripts/reap_mcp_orphans.sh"
    sleep 20   # wait out the TERM->KILL grace, then sweep the freed profiles
    sudo -u "$owner" CHROME_PROFILE_MAX_AGE_MIN=0 bash "$repo/scripts/reap_mcp_orphans.sh"
fi

if (( WITH_PNPM )); then
    echo "== 4. move pnpm store off tmpfs"
    pnpm_bin="$(sudo -u "$owner" -i bash -lc 'command -v pnpm' || true)"
    if [[ -n "$pnpm_bin" ]]; then
        run sudo -u "$owner" -i "$pnpm_bin" config set store-dir "/home/$owner/.local/share/pnpm/store" --global
    fi
    if pgrep -x pnpm >/dev/null; then
        echo "  pnpm is running; skipping /tmp/.pnpm-store removal"
    elif [[ -d /tmp/.pnpm-store ]]; then
        run rm -rf --one-file-system /tmp/.pnpm-store
    fi
fi

echo "== after"
systemctl show chromadb-server -p MemoryCurrent -p MemorySwapCurrent -p MemorySwapMax | sed 's/^/  /'
free -m | sed 's/^/  /'
df -h /tmp | sed 's/^/  /'

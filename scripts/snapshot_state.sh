#!/usr/bin/env bash
# P0.25-a (docs/2026-07-03-grand-refactor-plan.md): consistent .state snapshot
# for the refactor train. Unlike backup_state.sh (weekly rsync of a LIVE tree,
# where SQLite/Chroma files can be copied mid-write), this uses the SQLite
# .backup API for every database so snapshots are consistent WITHOUT stopping
# the bot. Take one before every state/session-touching PR (P2.5 / P4).
#
# Usage:
#   scripts/snapshot_state.sh                 # take a snapshot
#   scripts/snapshot_state.sh --verify <dir>  # restore-rehearse an existing
#                                             # snapshot into a temp dir and
#                                             # integrity-check every DB
set -euo pipefail
umask 077

BOT_DIR="/home/ubuntu/discord-bot"
STATE_DIR="${BOT_DIR}/.state"
SNAP_ROOT="${HOME}/.local/state/discord-bot/refactor-snapshots"

# Skip: stale backups already inside .state, transient locks/pids, eval scratch.
EXCLUDES=(
    --exclude='*.bak*' --exclude='*.broken.*' --exclude='*_flush_backup_*'
    --exclude='*.lock' --exclude='bot.pid' --exclude='eval'
    --exclude='agy_workdir' --exclude='codex_workdir'
    --exclude='*.db-shm' --exclude='*.db-wal' --exclude='*-shm' --exclude='*-wal'
)

sqlite_files() {
    # Live SQLite databases (by extension) under a root, excluding stale copies.
    find "$1" -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) \
        | grep -vE '\.bak|\.broken\.|_flush_backup_' || true
}

WORK=""

verify_snapshot() {
    local snap="$1"
    [[ -d "${snap}" ]] || { echo "[verify] snapshot not found: ${snap}"; exit 2; }
    # WORK is global and the trap is registered at definition time with the
    # value already expanded — an EXIT trap that references a function-local
    # fires after locals are gone and dies under set -u instead of cleaning up.
    WORK="$(mktemp -d /tmp/state_restore_rehearsal.XXXXXX)"
    # shellcheck disable=SC2064  # early expansion is deliberate
    trap "rm -rf '${WORK}'" EXIT
    local work="${WORK}"
    echo "[verify] restore rehearsal into ${work}"
    cp -a "${snap}/." "${work}/"
    local fail=0 checked=0
    while IFS= read -r db; do
        [[ -n "${db}" ]] || continue
        checked=$((checked + 1))
        result="$(sqlite3 "${db}" 'PRAGMA integrity_check;' 2>&1 | head -1)"
        if [[ "${result}" != "ok" ]]; then
            echo "[verify] FAIL ${db}: ${result}"
            fail=1
        fi
    done < <(sqlite_files "${work}")
    echo "[verify] integrity-checked ${checked} databases"
    # Spot-check the JSON state files parse.
    for j in sessions.json runtime_state.json; do
        if [[ -f "${work}/${j}" ]]; then
            python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${work}/${j}" \
                || { echo "[verify] FAIL ${j}: invalid JSON"; fail=1; }
        fi
    done
    if [[ "${fail}" -ne 0 ]]; then
        echo "[verify] REHEARSAL FAILED — do not trust this snapshot"
        exit 1
    fi
    echo "[verify] rehearsal OK — snapshot is restorable"
}

if [[ "${1:-}" == "--verify" ]]; then
    verify_snapshot "${2:?usage: --verify <snapshot-dir>}"
    exit 0
fi

STAMP="$(date +%Y%m%dT%H%M%S)"
DEST="${SNAP_ROOT}/${STAMP}"
mkdir -p "${DEST}"

echo "[snapshot] file tree (excluding live DBs, locks, scratch)"
rsync -a "${EXCLUDES[@]}" \
    --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' \
    "${STATE_DIR}/" "${DEST}/"

echo "[snapshot] SQLite databases via .backup API (consistent while live)"
while IFS= read -r db; do
    [[ -n "${db}" ]] || continue
    rel="${db#"${STATE_DIR}"/}"
    mkdir -p "${DEST}/$(dirname "${rel}")"
    sqlite3 "${db}" ".backup '${DEST}/${rel}'"
    echo "  - ${rel}"
done < <(sqlite_files "${STATE_DIR}")

git -C "${BOT_DIR}" rev-parse HEAD > "${DEST}/GIT_COMMIT"
du -sh "${DEST}" | awk '{print "[snapshot] size: "$1}'
echo "[snapshot] done: ${DEST}"
echo "[snapshot] verify with: scripts/snapshot_state.sh --verify ${DEST}"

# --- C1 (Cloudflare offload): mirror the snapshot to R2 ---------------------
# Requires R2 to be enabled on the account (dashboard, one-time) and the
# bucket:  wrangler r2 bucket create kanae-state-backup
# Off-machine copy = the .state survives total VPS loss. Controlled by env so
# the local snapshot still works before R2 is enabled.
if [[ "${SNAPSHOT_R2_SYNC:-1}" == "1" ]] && command -v wrangler >/dev/null 2>&1; then
    export PATH="$HOME/.volta/bin:$PATH"
    R2_ARCHIVE="/tmp/state-snapshot-${STAMP}.tar.gz"
    tar czf "${R2_ARCHIVE}" -C "${DEST}" .
    if wrangler r2 object put \
        "kanae-state-backup/snapshots/${STAMP}.tar.gz" \
        --file "${R2_ARCHIVE}" --remote >/dev/null 2>&1; then
        echo "[snapshot] R2 mirror OK: snapshots/${STAMP}.tar.gz ($(du -h "${R2_ARCHIVE}" | cut -f1))"
    else
        echo "[snapshot] R2 mirror FAILED (R2 enabled? bucket exists?) — local snapshot is still valid"
    fi
    rm -f "${R2_ARCHIVE}"
fi

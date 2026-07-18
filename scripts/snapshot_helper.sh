#!/usr/bin/env bash
# Install root-owned as /usr/local/libexec/discord-bot-snapshot.
# This is the only privileged entry point used by the production release gate.
set -euo pipefail
umask 077

IMPL=/usr/local/libexec/discord-bot-snapshot-impl
SNAP_ROOT=/var/lib/discord-bot-backup/release-gate-root
INVENTORY=/etc/discord-bot/backup-state.inventory
RELEASE_METADATA=/var/lib/discord-bot-releases/prod/current/release.json

if [[ ! -f "${IMPL}" || -L "${IMPL}" || ! -x "${IMPL}" \
    || "$(stat -c %u "${IMPL}")" != 0 \
    || -n "$(find "${IMPL}" -maxdepth 0 -perm /022 -print -quit)" ]]; then
    echo "[snapshot-helper] installed implementation is not root-protected" >&2
    exit 2
fi

if [[ "$#" -eq 0 ]]; then
    exec env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        HOME=/root \
        SNAPSHOT_STATE_INVENTORY="${INVENTORY}" \
        SNAPSHOT_RELEASE_METADATA_PATH="${RELEASE_METADATA}" \
        SNAPSHOT_INVENTORY_OWNER_UID=0 \
        SNAPSHOT_REQUIRE_READ_ONLY=0 \
        SNAPSHOT_R2_SYNC=0 \
        SNAPSHOT_ROOT="${SNAP_ROOT}" \
        "${IMPL}"
fi

if [[ "$#" -eq 2 && "$1" == --verify ]]; then
    [[ ! -L "$2" ]] || {
        echo "[snapshot-helper] verification target cannot be a symlink" >&2
        exit 2
    }
    root_real="$(realpath -e -- "${SNAP_ROOT}")"
    snapshot_real="$(realpath -e -- "$2")" || exit 2
    if [[ "${snapshot_real}" != "${root_real}"/* || ! -d "${snapshot_real}" ]]; then
        echo "[snapshot-helper] verification target is outside the fixed root" >&2
        exit 2
    fi
    exec env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        HOME=/root \
        SNAPSHOT_ROOT="${SNAP_ROOT}" \
        "${IMPL}" --verify "${snapshot_real}"
fi

echo "[snapshot-helper] usage: discord-bot-snapshot [--verify SNAPSHOT]" >&2
exit 2

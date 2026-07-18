#!/usr/bin/env bash
# Operational release gate shared by automatic and manual deployment.
#
# dev:  run smoke tests against the exact checked-out target.
# prod: require an exact-SHA staging receipt, a healthy staging readiness
#       payload, a protected-path manifest rooted outside the checkout, and a
#       generation-consistent snapshot restore rehearsal.
set -euo pipefail
umask 077

MODE="${1:-}"
TARGET_SHA="${2:-}"
if [[ "${MODE}" != "prod" && "${MODE}" != "dev" ]]; then
    echo "[release_gate] usage: $0 prod|dev <40-char-target-sha>" >&2
    exit 2
fi
if ! [[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "[release_gate/${MODE}] invalid target SHA: ${TARGET_SHA}" >&2
    exit 2
fi

if [[ "${MODE}" == "prod" ]]; then
    BOT_DIR="${RELEASE_GATE_BOT_DIR:-/home/ubuntu/discord-bot}"
else
    BOT_DIR="${RELEASE_GATE_BOT_DIR:-/home/ubuntu/discord-bot-dev}"
fi

log() { echo "[release_gate/${MODE}] $*"; }

[[ -d "${BOT_DIR}/.git" ]] || {
    log "repository missing: ${BOT_DIR}"
    exit 3
}

if [[ "${MODE}" == "dev" ]]; then
    current_sha="$(git -C "${BOT_DIR}" rev-parse HEAD)"
    if [[ "${current_sha}" != "${TARGET_SHA}" ]]; then
        log "checked-out SHA ${current_sha} does not match target ${TARGET_SHA}"
        exit 3
    fi
    smoke_script="${RELEASE_GATE_SMOKE_SCRIPT:-${BOT_DIR}/scripts/smoke_bot.sh}"
    release_venv="${RELEASE_GATE_VENV:-}"
    [[ "${release_venv}" == /* && -x "${release_venv}/bin/python" \
        && -f "${release_venv}/.release-sha" \
        && "$(tr -d '[:space:]' <"${release_venv}/.release-sha")" == "${TARGET_SHA}" ]] || {
        log "SHA-specific staging venv is missing or does not match target"
        exit 3
    }
    release_venv_real="$(realpath -e "${release_venv}")"
    bot_dir_real="$(realpath -e "${BOT_DIR}")"
    if [[ "${release_venv_real}" == "${bot_dir_real}" \
        || "${release_venv_real}" == "${bot_dir_real}"/* ]]; then
        log "staging venv must be outside the checkout"
        exit 3
    fi
    [[ -x "${smoke_script}" ]] || {
        log "smoke script missing or not executable: ${smoke_script}"
        exit 3
    }
    log "running staging smoke on ${TARGET_SHA:0:8}"
    BOT_DIR="${BOT_DIR}" BOT_VENV="${release_venv}" "${smoke_script}"
    log "staging smoke passed"
    exit 0
fi

trusted_state_root="${RELEASE_GATE_TRUSTED_STATE_ROOT:-/var/lib/discord-bot-deploy}"
staging_receipt="${RELEASE_GATE_STAGING_RECEIPT:-${trusted_state_root}/dev/successful.sha}"
staging_ready_url="${RELEASE_GATE_STAGING_READY_URL:-http://127.0.0.1:3003/ready}"
manifest="${RELEASE_GATE_PROTECTED_MANIFEST:-/etc/discord-bot/release/protected-manifest.sha256}"
state_inventory="${RELEASE_GATE_STATE_INVENTORY:-/etc/discord-bot/backup-state.inventory}"
release_metadata="${RELEASE_GATE_RELEASE_METADATA_PATH:-/var/lib/discord-bot-releases/prod/current/release.json}"
snapshot_root="${RELEASE_GATE_SNAPSHOT_ROOT:-/var/lib/discord-bot-backup/release-gate}"

# Receipt state must not live in either mutable application checkout.  The
# deploy identity owns trusted_state_root; bot service identities do not.
receipt_parent="$(realpath -m "$(dirname "${staging_receipt}")")"
trusted_state_real="$(realpath -m "${trusted_state_root}")"
if [[ "${receipt_parent}" != "${trusted_state_real}"/* ]]; then
    log "staging receipt is outside trusted deploy state: ${staging_receipt}"
    exit 3
fi
if [[ ! -s "${staging_receipt}" ]]; then
    log "staging success receipt is missing: ${staging_receipt}"
    exit 3
fi
staging_sha="$(head -1 "${staging_receipt}" | tr -d '[:space:]')"
if ! [[ "${staging_sha}" =~ ^[0-9a-f]{40}$ ]]; then
    log "staging receipt contains an invalid SHA"
    exit 3
fi
if [[ "${staging_sha}" != "${TARGET_SHA}" ]]; then
    log "staging SHA ${staging_sha:0:8} does not exactly match target ${TARGET_SHA:0:8}"
    exit 3
fi

ready_payload="$(curl -fsS --max-time 5 -- "${staging_ready_url}")" || {
    log "staging is not ready: ${staging_ready_url}"
    exit 3
}
if ! python3 -c '
import json, sys
try:
    payload = json.loads(sys.stdin.read())
except (json.JSONDecodeError, UnicodeError):
    raise SystemExit(1)
target = sys.argv[1]
if not isinstance(payload, dict):
    raise SystemExit(1)
if payload.get("status") != "ok" or payload.get("release_sha") != target:
    raise SystemExit(1)
' "${TARGET_SHA}" <<<"${ready_payload}"; then
    log "staging readiness payload is not status=ok at target ${TARGET_SHA:0:8}"
    exit 3
fi
log "staging receipt and readiness passed (${staging_sha:0:8})"

# The protected manifest is trusted only when its digest is supplied by the
# installed unit/environment, or when it is root-owned and not group/other
# writable.  A manifest changed in the target commit cannot approve itself.
if [[ ! -f "${manifest}" ]]; then
    log "protected manifest is missing: ${manifest}"
    exit 3
fi
manifest_real="$(realpath -e "${manifest}")"
case "${manifest_real}" in
    "$(realpath -e "${BOT_DIR}")"/*)
        log "protected manifest must be rooted outside the checkout"
        exit 3
        ;;
esac
manifest_digest="$(sha256sum "${manifest_real}" | awk '{print $1}')"
expected_manifest_digest="${RELEASE_GATE_PROTECTED_MANIFEST_SHA256:-}"
if [[ -n "${expected_manifest_digest}" ]]; then
    if ! [[ "${expected_manifest_digest}" =~ ^[0-9a-f]{64}$ ]] \
        || [[ "${manifest_digest}" != "${expected_manifest_digest}" ]]; then
        log "protected manifest digest does not match the external pin"
        exit 3
    fi
elif [[ "$(stat -c %u "${manifest_real}")" != "0" ]] \
    || [[ -n "$(find "${manifest_real}" -maxdepth 0 -perm /022 -print -quit)" ]]; then
    log "protected manifest must be root-owned and not group/other writable"
    exit 3
fi

if ! python3 - "${BOT_DIR}" "${TARGET_SHA}" "${manifest_real}" <<'PY'
import hashlib
import pathlib
import re
import subprocess
import sys

repo, target, manifest_path = sys.argv[1:]
entries = 0
for raw in pathlib.Path(manifest_path).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    try:
        expected, relative = line.split(maxsplit=1)
    except ValueError as exc:
        raise SystemExit(f"invalid protected manifest line: {line}") from exc
    relative = relative.strip()
    path = pathlib.PurePosixPath(relative)
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SystemExit(f"invalid protected digest: {relative}")
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise SystemExit(f"unsafe protected path: {relative}")
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{target}:{path}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise SystemExit(f"protected path missing at target: {relative}")
    if hashlib.sha256(result.stdout).hexdigest() != expected:
        raise SystemExit(f"protected path mismatch at target: {relative}")
    entries += 1
if entries == 0:
    raise SystemExit("protected manifest has no entries")
PY
then
    log "protected manifest verification failed for ${TARGET_SHA:0:8}"
    exit 3
fi
log "externally pinned protected manifest passed"

snapshot_script="${RELEASE_GATE_SNAPSHOT_SCRIPT:-${BOT_DIR}/scripts/snapshot_state.sh}"
[[ -x "${snapshot_script}" ]] || {
    log "snapshot script missing or not executable: ${snapshot_script}"
    exit 3
}

log "creating a generation-consistent local snapshot with remote sync disabled"
if [[ -r "${release_metadata}" ]]; then
    snapshot_output="$(SNAPSHOT_STATE_INVENTORY="${state_inventory}" \
        SNAPSHOT_RELEASE_METADATA_PATH="${release_metadata}" \
        SNAPSHOT_INVENTORY_OWNER_UID=0 SNAPSHOT_REQUIRE_READ_ONLY=1 SNAPSHOT_R2_SYNC=0 \
        SNAPSHOT_ROOT="${snapshot_root}" \
        "${snapshot_script}")"
else
    snapshot_output="$(SNAPSHOT_STATE_INVENTORY="${state_inventory}" \
        SNAPSHOT_INVENTORY_OWNER_UID=0 SNAPSHOT_REQUIRE_READ_ONLY=1 \
        SNAPSHOT_ROOT="${snapshot_root}" SNAPSHOT_R2_SYNC=0 "${snapshot_script}")"
fi
printf '%s\n' "${snapshot_output}"
snapshot_dir="$(printf '%s\n' "${snapshot_output}" \
    | sed -n 's/^\[snapshot\] done: //p' | tail -1)"
if [[ -z "${snapshot_dir}" || ! -d "${snapshot_dir}" ]]; then
    log "snapshot output did not identify a valid snapshot directory"
    exit 3
fi

log "restore-rehearsing ${snapshot_dir}"
"${snapshot_script}" --verify "${snapshot_dir}"
log "backup/restore rehearsal passed"

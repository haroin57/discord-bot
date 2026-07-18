#!/usr/bin/env bash
# Trusted rollback runner. Install this file root-owned outside the checkout.
# Usage: bot-rollback.sh <tag> <expected-old-sha> <blocked-release-sha>
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${SCRIPT_DIR}" == "/usr/local/libexec" ]]; then
    RELEASE_LIB="${SCRIPT_DIR}/discord-bot-release-lib"
    if [[ "$(stat -c %u "${RELEASE_LIB}" 2>/dev/null || printf invalid)" != "0" ]] \
        || [[ -n "$(find "${RELEASE_LIB}" -maxdepth 0 -perm /022 -print -quit 2>/dev/null)" ]]; then
        echo "[rollback] installed release helper is not root-protected" >&2
        exit 4
    fi
else
    RELEASE_LIB="${SCRIPT_DIR}/release_artifact_lib.sh"
fi
# shellcheck source=scripts/release_artifact_lib.sh
source "${RELEASE_LIB}"

TAG="${1:-}"
EXPECTED_OLD_SHA="${2:-}"
BLOCKED_SHA="${3:-}"
BOT_DIR="${DEPLOY_BOT_DIR:-/home/ubuntu/discord-bot}"
SERVICE="${DEPLOY_SERVICE:-discord-bot}"
DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR:-/var/lib/discord-bot-deploy/prod}"
LOCK_FILE="${DEPLOY_LOCK_FILE:-${DEPLOY_STATE_DIR}/deploy.lock}"
PIN_FILE="${DEPLOY_ROLLBACK_PIN_FILE:-${DEPLOY_STATE_DIR}/rollback.pin}"
READY_ATTEMPTS="${DEPLOY_READY_ATTEMPTS:-36}"
READY_INTERVAL_SECONDS="${DEPLOY_READY_INTERVAL_SECONDS:-5}"
RELEASE_ROOT="${DEPLOY_RELEASE_ROOT:-/var/lib/discord-bot-releases/prod}"
RELEASE_GENERATIONS_ROOT="${DEPLOY_GENERATIONS_ROOT:-${RELEASE_ROOT}/generations}"
RELEASE_CURRENT_LINK="${DEPLOY_CURRENT_LINK:-${RELEASE_ROOT}/current}"
RELEASE_BUILD_ROOT="${DEPLOY_BUILD_ROOT:-${DEPLOY_STATE_DIR}/release-build}"
RELEASE_ACTIVATION_JOURNAL="${DEPLOY_ACTIVATION_JOURNAL:-${DEPLOY_STATE_DIR}/activation.json}"
RELEASE_PUBLISHER="${DEPLOY_RELEASE_PUBLISHER:-/usr/local/libexec/discord-bot-release-publisher}"
RELEASE_PUBLISHER_USE_SUDO="${DEPLOY_PUBLISHER_USE_SUDO:-1}"
RELEASE_GENERATION_OWNER_UID="${DEPLOY_GENERATION_OWNER_UID:-0}"
RELEASE_CONSUMERS="${DEPLOY_CONSUMERS:-chromadb-server mcp-mem0-shared mcp-second-opinion-shared mcp-oracle-shared vi-signal-scheduler intervention-scheduler hermes-discord-codex discord-bot kanae-mcp-forward kanae-systemd-forward}"
RELEASE_READY_PORTS="${DEPLOY_READY_PORTS:-3004 3002}"
RELEASE_LIVENESS_URLS="${DEPLOY_LIVENESS_URLS:-http://127.0.0.1:8765/api/v2/heartbeat}"
if ! [[ "${SERVICE}" =~ ^[A-Za-z0-9@_.-]+$ ]]; then
    echo "[rollback] invalid service configuration" >&2
    exit 2
fi

if [[ -z "${TAG}" || -z "${EXPECTED_OLD_SHA}" || -z "${BLOCKED_SHA}" ]]; then
    echo "Usage: $0 <tag-name> <expected-old-sha> <blocked-release-sha>" >&2
    exit 1
fi
if ! [[ "${TAG}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
    echo "[rollback] invalid tag name: ${TAG}" >&2
    exit 2
fi
if ! [[ "${EXPECTED_OLD_SHA}" =~ ^[0-9a-f]{40}$ ]] \
    || ! [[ "${BLOCKED_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "[rollback] expected and blocked release SHAs must be full 40-character hashes" >&2
    exit 2
fi
if ! [[ "${READY_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]] \
    || ! [[ "${READY_INTERVAL_SECONDS}" =~ ^[0-9]+$ ]]; then
    echo "[rollback] invalid readiness threshold" >&2
    exit 2
fi

log_readiness_diagnostics() {
    local expected_sha="$1" port="$2" outcome="$3" body="$4" compact
    compact="$(printf '%s' "${body}" | tr '\r\n\t' '   ')"
    echo "[rollback] readiness diagnostic port=${port} expected=${expected_sha:0:12} ${outcome} body=${compact:0:4096}" >&2
}

ready_matches_release() {
    local expected_sha="$1" port response payload tail http_status rc
    for port in ${RELEASE_READY_PORTS}; do
        response=""
        rc=0
        response="$(curl -sS --fail-with-body --max-time 5 \
            --write-out $'\n%{http_code}' -- \
            "http://127.0.0.1:${port}/ready" 2>&1)" || rc=$?
        tail="${response##*$'\n'}"
        if [[ "${tail}" =~ ^[0-9]{3}$ ]]; then
            http_status="${tail}"
            payload="${response%$'\n'*}"
        else
            http_status="unknown"
            payload="${response}"
        fi
        if ((rc != 0)); then
            log_readiness_diagnostics "${expected_sha}" "${port}" \
                "curl_rc=${rc} http=${http_status}" "${payload}"
            return 1
        fi
        if [[ "${http_status}" != "unknown" && ! "${http_status}" =~ ^2[0-9]{2}$ ]]; then
            log_readiness_diagnostics "${expected_sha}" "${port}" \
                "http=${http_status}" "${payload}"
            return 1
        fi
        if ! /usr/bin/python3 -c '
import json, sys
try:
    payload = json.loads(sys.stdin.read())
except (json.JSONDecodeError, UnicodeError):
    raise SystemExit(1)
expected = sys.argv[1]
if not isinstance(payload, dict):
    raise SystemExit(1)
if payload.get("status") != "ok" or payload.get("release_sha") != expected:
    raise SystemExit(1)
' "${expected_sha}" <<<"${payload}"; then
            log_readiness_diagnostics "${expected_sha}" "${port}" \
                "http=${http_status} invalid_payload=1" "${payload}"
            return 1
        fi
    done
    release_consumers_active && release_liveness_urls_ready
}

quarantine_release_artifacts() {
    local expected_sha="$1" reason="$2" dirty_status quarantine_dir path target
    if [[ "$(git rev-parse HEAD)" != "${expected_sha}" ]]; then
        echo "[rollback] cannot quarantine artifacts after HEAD changed" >&2
        return 1
    fi
    dirty_status="$(git status --porcelain=v1 --untracked-files=all)" || return 1
    if [[ -z "${dirty_status}" ]]; then
        return 0
    fi
    quarantine_dir="${DEPLOY_STATE_DIR}/quarantine/${expected_sha:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    mkdir -p "${quarantine_dir}/untracked"
    chmod 700 "${quarantine_dir}" "${quarantine_dir}/untracked"
    printf '%s\n' "${reason}" >"${quarantine_dir}/reason.txt"
    printf '%s\n' "${dirty_status}" >"${quarantine_dir}/status.txt"
    git diff --binary --no-ext-diff >"${quarantine_dir}/tracked.patch"
    git diff --cached --binary --no-ext-diff >"${quarantine_dir}/staged.patch"
    while IFS= read -r -d '' path; do
        target="${quarantine_dir}/untracked/${path}"
        mkdir -p "$(dirname "${target}")"
        mv -- "${path}" "${target}"
    done < <(git ls-files --others --exclude-standard -z)
    git restore --source="${expected_sha}" --staged --worktree -- .
    if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
        echo "[rollback] artifact quarantine did not restore a clean checkout" >&2
        return 1
    fi
    chmod -R go-rwx "${quarantine_dir}"
    echo "[rollback] quarantined dependency artifacts at ${quarantine_dir}" >&2
}

cd "${BOT_DIR}"
mkdir -p "${DEPLOY_STATE_DIR}"
chmod 700 "${DEPLOY_STATE_DIR}"
bot_real="$(realpath -e "${BOT_DIR}")"
state_real="$(realpath -e "${DEPLOY_STATE_DIR}")"
if [[ "${state_real}" == "${bot_real}" || "${state_real}" == "${bot_real}"/* ]]; then
    echo "[rollback] deploy trust state must be outside the application checkout" >&2
    exit 4
fi
for trusted_file in "${LOCK_FILE}" "${PIN_FILE}" "${RELEASE_ACTIVATION_JOURNAL}"; do
    if [[ "$(realpath -m "$(dirname "${trusted_file}")")" != "${state_real}" ]]; then
        echo "[rollback] deploy lock and pin must be direct children of trusted state" >&2
        exit 4
    fi
done

# This runner always acquires the lock itself.  There is no environment flag
# that can bypass serialization from an interactive or compromised caller.
command -v flock >/dev/null 2>&1 || {
    echo "[rollback] flock is required" >&2
    exit 4
}
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[rollback] another deploy or rollback is running" >&2
    exit 4
fi
release_artifact_init || exit 4
if ! validate_release_coordination; then
    echo "[rollback] invalid consumer or readiness configuration" >&2
    exit 4
fi
if ! release_consumers_provisioned; then
    echo "[rollback] required release consumer is not provisioned" >&2
    exit 4
fi
RELEASE_RECOVERY_ATTEMPTS="${READY_ATTEMPTS}"
RELEASE_RECOVERY_INTERVAL_SECONDS="${READY_INTERVAL_SECONDS}"

dirty_status="$(git status --porcelain=v1 --untracked-files=all)" || {
    echo "[rollback] cannot determine worktree status" >&2
    exit 4
}
if [[ -n "${dirty_status}" ]]; then
    echo "[rollback] dirty worktree; refusing to overwrite local changes" >&2
    printf '%s\n' "${dirty_status}" >&2
    exit 4
fi

CURRENT_SHA="$(git rev-parse HEAD)"
if [[ "${CURRENT_SHA}" != "${BLOCKED_SHA}" \
    && "${CURRENT_SHA}" != "${EXPECTED_OLD_SHA}" ]]; then
    echo "[rollback] HEAD ${CURRENT_SHA} matches neither blocked nor target release" >&2
    exit 2
fi
TAG_SHA="$(git rev-parse --verify "refs/tags/${TAG}^{commit}" 2>/dev/null)" || {
    echo "[rollback] tag not found or not a commit: ${TAG}" >&2
    exit 2
}
if [[ "${TAG_SHA}" != "${EXPECTED_OLD_SHA}" ]]; then
    echo "[rollback] tag ${TAG} resolves to ${TAG_SHA}, expected ${EXPECTED_OLD_SHA}" >&2
    exit 2
fi
if ! git cat-file -e "${EXPECTED_OLD_SHA}^{commit}" 2>/dev/null; then
    echo "[rollback] expected old SHA is not a commit: ${EXPECTED_OLD_SHA}" >&2
    exit 2
fi
if ! build_release_venv "${EXPECTED_OLD_SHA}"; then
    echo "[rollback] verified old release environment could not be built" >&2
    exit 4
fi

write_pin() {
    local pin_tmp="${PIN_FILE}.tmp.$$"
    {
        printf 'target_sha=%s\n' "${EXPECTED_OLD_SHA}"
        printf 'tag=%s\n' "${TAG}"
        printf 'blocked_sha=%s\n' "${BLOCKED_SHA}"
        printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"${pin_tmp}"
    chmod 600 "${pin_tmp}"
    mv -f "${pin_tmp}" "${PIN_FILE}"
}

if [[ -s "${PIN_FILE}" ]]; then
    pin_target="$(sed -n 's/^target_sha=//p' "${PIN_FILE}")"
    pin_tag="$(sed -n 's/^tag=//p' "${PIN_FILE}")"
    pin_blocked="$(sed -n 's/^blocked_sha=//p' "${PIN_FILE}")"
    if [[ "${pin_target}" != "${EXPECTED_OLD_SHA}" \
        || "${pin_tag}" != "${TAG}" \
        || "${pin_blocked}" != "${BLOCKED_SHA}" ]]; then
        echo "[rollback] existing rollback pin does not match this recovery contract" >&2
        exit 4
    fi
else
    write_pin
fi

# Close the last mutable-state window before moving HEAD.  A retag, another
# checkout, or a bot write converts recovery into a fail-closed operator event.
dirty_status="$(git status --porcelain=v1 --untracked-files=all)"
current_validated_sha="$(git rev-parse HEAD)"
if [[ -n "${dirty_status}" ]] \
    || [[ "${current_validated_sha}" != "${BLOCKED_SHA}" \
        && "${current_validated_sha}" != "${EXPECTED_OLD_SHA}" ]] \
    || [[ "$(git rev-parse --verify "refs/tags/${TAG}^{commit}")" != "${EXPECTED_OLD_SHA}" ]]; then
    echo "[rollback] repository changed after rollback validation" >&2
    exit 4
fi

echo "[rollback] switching to verified ${TAG} (${EXPECTED_OLD_SHA})"
if ! begin_release_transaction rollback "${BLOCKED_SHA}" "${EXPECTED_OLD_SHA}" 1; then
    echo "[rollback] failed to persist rollback activation transaction" >&2
    exit 4
fi
git switch --detach "${EXPECTED_OLD_SHA}"
git clean -ffdx --quiet

if [[ "$(git rev-parse HEAD)" != "${EXPECTED_OLD_SHA}" ]] \
    || [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
    echo "[rollback] old release is not clean; refusing restart" >&2
    exit 4
fi
for private_name in .env .state .claude .venv; do
    if [[ -e "${BOT_DIR}/${private_name}" || -L "${BOT_DIR}/${private_name}" ]]; then
        echo "[rollback] verified old release contains forbidden private path ${private_name}" >&2
        exit 4
    fi
done

if ! activate_release_transaction "${EXPECTED_OLD_SHA}"; then
    echo "[rollback] failed to atomically activate the verified old release" >&2
    exit 4
fi

restart_release_consumers_transactional

for ((i = 1; i <= READY_ATTEMPTS; i++)); do
    sleep "${READY_INTERVAL_SECONDS}"
    if ready_matches_release "${EXPECTED_OLD_SHA}"; then
        complete_release_transaction || exit 4
        echo "[rollback] ready at ${TAG}; rollback pin remains active"
        exit 0
    fi
    echo "[rollback] readiness attempt ${i}/${READY_ATTEMPTS} failed" >&2
done

echo "[rollback] target restarted but did not become ready; pin remains active" >&2
exit 3

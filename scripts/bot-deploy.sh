#!/usr/bin/env bash
# Manual production deploy using the same trust boundaries as auto_deploy.sh.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release_artifact_lib.sh
source "${SCRIPT_DIR}/release_artifact_lib.sh"

BOT_DIR="${DEPLOY_BOT_DIR:-/var/lib/discord-bot-deploy/checkouts/prod}"
SERVICE="${DEPLOY_SERVICE:-discord-bot}"
DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR:-/var/lib/discord-bot-deploy/prod}"
LOCK_FILE="${DEPLOY_LOCK_FILE:-${DEPLOY_STATE_DIR}/deploy.lock}"
PIN_FILE="${DEPLOY_ROLLBACK_PIN_FILE:-${DEPLOY_STATE_DIR}/rollback.pin}"
RELEASE_GATE="${DEPLOY_RELEASE_GATE:-${BOT_DIR}/scripts/release_gate.sh}"
TRUSTED_ROLLBACK_RUNNER="${DEPLOY_TRUSTED_ROLLBACK_RUNNER:-/usr/local/libexec/discord-bot-rollback}"
TRUSTED_ROLLBACK_RUNNER_SHA256="${DEPLOY_TRUSTED_ROLLBACK_RUNNER_SHA256:-}"
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
    echo "[deploy] invalid service configuration" >&2
    exit 2
fi

verify_trusted_runner() {
    local runner_real runner_digest
    [[ -x "${TRUSTED_ROLLBACK_RUNNER}" ]] || {
        echo "[deploy] trusted rollback runner is missing" >&2
        return 1
    }
    runner_real="$(realpath -e "${TRUSTED_ROLLBACK_RUNNER}")" || return 1
    case "${runner_real}" in
        "$(realpath -e "${BOT_DIR}")"/*)
            echo "[deploy] trusted rollback runner must be outside the checkout" >&2
            return 1
            ;;
    esac
    runner_digest="$(sha256sum "${runner_real}" | awk '{print $1}')"
    if [[ -n "${TRUSTED_ROLLBACK_RUNNER_SHA256}" ]]; then
        if ! [[ "${TRUSTED_ROLLBACK_RUNNER_SHA256}" =~ ^[0-9a-f]{64}$ ]] \
            || [[ "${runner_digest}" != "${TRUSTED_ROLLBACK_RUNNER_SHA256}" ]]; then
            echo "[deploy] trusted rollback runner digest mismatch" >&2
            return 1
        fi
    elif [[ "$(stat -c %u "${runner_real}")" != "0" ]] \
        || [[ -n "$(find "${runner_real}" -maxdepth 0 -perm /022 -print -quit)" ]]; then
        echo "[deploy] trusted rollback runner must be root-owned and protected" >&2
        return 1
    fi
    VERIFIED_ROLLBACK_RUNNER="${runner_real}"
    VERIFIED_ROLLBACK_RUNNER_DIGEST="${runner_digest}"
}

write_pin() {
    local pin_tmp="${PIN_FILE}.tmp.$$"
    {
        printf 'target_sha=%s\n' "${PREV}"
        printf 'tag=%s\n' "${TAG}"
        printf 'blocked_sha=%s\n' "${NEW}"
        printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"${pin_tmp}"
    chmod 600 "${pin_tmp}"
    mv -f "${pin_tmp}" "${PIN_FILE}"
}

log_readiness_diagnostics() {
    local expected_sha="$1" port="$2" outcome="$3" body="$4" compact
    compact="$(printf '%s' "${body}" | tr '\r\n\t' '   ')"
    echo "[deploy] readiness diagnostic port=${port} expected=${expected_sha:0:12} ${outcome} body=${compact:0:4096}" >&2
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
            # Test doubles and older curl wrappers may omit --write-out.
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
        echo "[deploy] cannot quarantine artifacts after HEAD changed" >&2
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
    if [[ "$(git rev-parse HEAD)" != "${expected_sha}" ]] \
        || [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
        echo "[deploy] artifact quarantine did not restore a clean checkout" >&2
        return 1
    fi
    chmod -R go-rwx "${quarantine_dir}"
    echo "[deploy] quarantined failed-release artifacts at ${quarantine_dir}" >&2
}

rollback_failed_deploy() {
    local reason="$1"
    echo "[deploy] ${reason}; automatically rolling back ${NEW} to ${PREV}" >&2
    if ! quarantine_release_artifacts "${NEW}" "${reason}"; then
        write_pin
        echo "[deploy] artifact quarantine failed; checkout remains pinned" >&2
        return 5
    fi
    if [[ "$(sha256sum "${VERIFIED_ROLLBACK_RUNNER}" | awk '{print $1}')" \
        != "${VERIFIED_ROLLBACK_RUNNER_DIGEST}" ]]; then
        echo "[deploy] trusted rollback runner changed after validation" >&2
        return 5
    fi
    write_pin
    flock -u 9
    exec 9>&-
        DEPLOY_BOT_DIR="${BOT_DIR}" \
        DEPLOY_SERVICE="${SERVICE}" \
        DEPLOY_CONSUMERS="${RELEASE_CONSUMERS}" \
        DEPLOY_READY_PORTS="${RELEASE_READY_PORTS}" \
        DEPLOY_LIVENESS_URLS="${RELEASE_LIVENESS_URLS}" \
        DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR}" \
        DEPLOY_LOCK_FILE="${LOCK_FILE}" \
        DEPLOY_ROLLBACK_PIN_FILE="${PIN_FILE}" \
        DEPLOY_READY_ATTEMPTS="${READY_ATTEMPTS}" \
        DEPLOY_READY_INTERVAL_SECONDS="${READY_INTERVAL_SECONDS}" \
        DEPLOY_RELEASE_ROOT="${RELEASE_ROOT}" \
        DEPLOY_GENERATIONS_ROOT="${RELEASE_GENERATIONS_ROOT}" \
        DEPLOY_CURRENT_LINK="${RELEASE_CURRENT_LINK}" \
        DEPLOY_BUILD_ROOT="${RELEASE_BUILD_ROOT}" \
        DEPLOY_ACTIVATION_JOURNAL="${RELEASE_ACTIVATION_JOURNAL}" \
        DEPLOY_RELEASE_PUBLISHER="${RELEASE_PUBLISHER}" \
        DEPLOY_PUBLISHER_USE_SUDO="${RELEASE_PUBLISHER_USE_SUDO}" \
        DEPLOY_GENERATION_OWNER_UID="${RELEASE_GENERATION_OWNER_UID}" \
        RELEASE_TEST_PUBLISHER="${RELEASE_TEST_PUBLISHER:-0}" \
        "${VERIFIED_ROLLBACK_RUNNER}" "${TAG}" "${PREV}" "${NEW}"
}

prepare_clean_checkout() {
    local migration_root item name dirty_status
    migration_root="${DEPLOY_STATE_DIR}/legacy-private/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    for name in .env .state .claude; do
        item="${BOT_DIR}/${name}"
        if [[ -e "${item}" || -L "${item}" ]]; then
            mkdir -p "${migration_root}"
            chmod 0700 "${DEPLOY_STATE_DIR}/legacy-private" "${migration_root}"
            mv -- "${item}" "${migration_root}/${name}"
            echo "[deploy] migrated legacy private checkout data: ${name}" >&2
        fi
    done
    dirty_status="$(git status --porcelain=v1 --untracked-files=all)" || return 1
    [[ -z "${dirty_status}" ]] || return 2
    git clean -ffdx --quiet
    for name in .env .state .claude .venv; do
        [[ ! -e "${BOT_DIR}/${name}" && ! -L "${BOT_DIR}/${name}" ]] || return 1
    done
}

restore_mismatched_merge() {
    local observed_sha="$1" reason="$2" quarantine_dir path target dirty_status
    dirty_status="$(git status --porcelain=v1 --untracked-files=all 2>/dev/null || true)"
    quarantine_dir="${DEPLOY_STATE_DIR}/quarantine/${NEW:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    mkdir -p "${quarantine_dir}/untracked"
    chmod 0700 "${quarantine_dir}" "${quarantine_dir}/untracked"
    printf '%s\n' "${reason}" >"${quarantine_dir}/reason.txt"
    printf '%s\n' "${observed_sha}" >"${quarantine_dir}/observed-sha.txt"
    printf '%s\n' "${dirty_status}" >"${quarantine_dir}/status.txt"
    git diff --binary --no-ext-diff >"${quarantine_dir}/tracked.patch" || true
    git diff --cached --binary --no-ext-diff >"${quarantine_dir}/staged.patch" || true
    while IFS= read -r -d '' path; do
        target="${quarantine_dir}/untracked/${path}"
        mkdir -p "$(dirname "${target}")"
        mv -- "${path}" "${target}"
    done < <(git ls-files --others --exclude-standard -z)
    write_pin
    begin_release_transaction rollback "${NEW}" "${PREV}" 1 || return 1
    git reset --hard "${PREV}" || return 1
    git clean -ffdx --quiet || return 1
    activate_release_transaction "${PREV}" || return 1
    chmod -R go-rwx "${quarantine_dir}"
    [[ "$(git rev-parse HEAD)" == "${PREV}" ]] \
        && [[ -z "$(git status --porcelain=v1 --untracked-files=all)" ]]
}

cd "${BOT_DIR}"
mkdir -p "${DEPLOY_STATE_DIR}"
chmod 700 "${DEPLOY_STATE_DIR}"
bot_real="$(realpath -e "${BOT_DIR}")"
state_real="$(realpath -e "${DEPLOY_STATE_DIR}")"
if [[ "${state_real}" == "${bot_real}" || "${state_real}" == "${bot_real}"/* ]]; then
    echo "[deploy] deploy trust state must be outside the application checkout" >&2
    exit 4
fi
for trusted_file in "${LOCK_FILE}" "${PIN_FILE}" "${RELEASE_ACTIVATION_JOURNAL}"; do
    if [[ "$(realpath -m "$(dirname "${trusted_file}")")" != "${state_real}" ]]; then
        echo "[deploy] deploy lock and pin must be direct children of trusted state" >&2
        exit 4
    fi
done
command -v flock >/dev/null 2>&1 || {
    echo "[deploy] flock is required; refusing deploy" >&2
    exit 4
}
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[deploy] another deploy or rollback is running" >&2
    exit 4
fi
release_artifact_init || exit 4
if ! validate_release_coordination; then
    echo "[deploy] invalid consumer or readiness configuration" >&2
    exit 4
fi
if ! release_consumers_provisioned; then
    echo "[deploy] required release consumer is not provisioned" >&2
    exit 4
fi
if ! [[ "${READY_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]] \
    || ! [[ "${READY_INTERVAL_SECONDS}" =~ ^[0-9]+$ ]]; then
    echo "[deploy] invalid readiness threshold" >&2
    exit 4
fi
RELEASE_RECOVERY_ATTEMPTS="${READY_ATTEMPTS}"
RELEASE_RECOVERY_INTERVAL_SECONDS="${READY_INTERVAL_SECONDS}"
if ! resume_release_transaction; then
    echo "[deploy] incomplete release activation could not be recovered" >&2
    exit 5
fi
if [[ -s "${PIN_FILE}" ]]; then
    echo "[deploy] rollback pin is active; investigate it before deploying" >&2
    exit 4
fi
PREPARE_RC=0
prepare_clean_checkout || PREPARE_RC=$?
if [[ "${PREPARE_RC}" -ne 0 && "${PREPARE_RC}" -ne 2 ]]; then
    echo "[deploy] could not sanitize deployment checkout" >&2
    exit 4
fi
dirty_status="$(git status --porcelain=v1 --untracked-files=all)" || {
    echo "[deploy] cannot determine worktree status" >&2
    exit 4
}
if [[ -n "${dirty_status}" ]]; then
    echo "[deploy] dirty worktree; refusing deploy" >&2
    printf '%s\n' "${dirty_status}" >&2
    exit 4
fi

git fetch origin master --quiet
PREV="$(git rev-parse HEAD)"
NEW="$(git rev-parse --verify 'FETCH_HEAD^{commit}')"
git cat-file -e "${NEW}^{commit}"
if [[ "${PREV}" == "${NEW}" ]]; then
    if release_current_matches_sha "${PREV}" && ready_matches_release "${PREV}"; then
        echo "[deploy] no changes; active generation and consumers match"
        exit 0
    fi
    echo "[deploy] no Git changes, but active generation or consumers do not match" >&2
    exit 5
fi
if ! git merge-base --is-ancestor "${PREV}" "${NEW}"; then
    echo "[deploy] origin/master is not a fast-forward of HEAD; refusing deploy" >&2
    exit 2
fi
if [[ ! -x "${RELEASE_GATE}" ]] || ! "${RELEASE_GATE}" prod "${NEW}"; then
    echo "[deploy] release gate failed; refusing deploy" >&2
    exit 4
fi
if ! verify_trusted_runner; then
    exit 4
fi
if ! build_release_venv "${PREV}" || ! build_release_venv "${NEW}"; then
    echo "[deploy] SHA-specific release environment validation failed" >&2
    exit 4
fi
if [[ -s "${PIN_FILE}" ]] \
    || [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
    echo "[deploy] rollback pin or dirty state appeared during release gate" >&2
    exit 4
fi

TAG="pre-deploy-$(date +%Y%m%d-%H%M%S)-${PREV:0:8}"
git tag "${TAG}" "${PREV}"
echo "[deploy] tagged ${PREV} as ${TAG}"

git merge --ff-only "${NEW}"
ACTUAL_NEW="$(git rev-parse HEAD)"
if [[ "${ACTUAL_NEW}" != "${NEW}" ]]; then
    echo "[deploy] merged SHA does not match gated fetched target; restoring old SHA" >&2
    restore_mismatched_merge "${ACTUAL_NEW}" "post-merge SHA mismatch" || {
        echo "[deploy] mismatch restore failed; rollback pin remains active" >&2
        exit 5
    }
    exit 5
fi

# This is deliberately adjacent to restart.  The bot service UID has only
# state/cache write access in systemd and cannot dirty .git or source files.
if [[ -s "${PIN_FILE}" ]] \
    || [[ "$(git rev-parse HEAD)" != "${NEW}" ]] \
    || [[ "$(git rev-parse --verify "refs/tags/${TAG}^{commit}")" != "${PREV}" ]]; then
    echo "[deploy] repository, rollback tag, or pin changed before restart" >&2
    if [[ ! -s "${PIN_FILE}" ]]; then
        restore_mismatched_merge "$(git rev-parse HEAD 2>/dev/null || printf unknown)" \
            "pre-restart integrity failure" || {
                echo "[deploy] pre-restart cleanup failed; pin remains active" >&2
                exit 5
            }
    fi
    exit 5
fi
if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
    restore_mismatched_merge "${NEW}" \
        "build or dependency step dirtied the merged checkout" || exit 5
    exit 5
fi
for private_name in .env .state .claude .venv; do
    if [[ -e "${BOT_DIR}/${private_name}" || -L "${BOT_DIR}/${private_name}" ]]; then
        echo "[deploy] merged release contains forbidden private path ${private_name}" >&2
        restore_mismatched_merge "${NEW}" "forbidden private checkout path" || exit 5
        exit 5
    fi
done

if ! begin_release_transaction deploy "${PREV}" "${NEW}"; then
    echo "[deploy] release activation transaction could not be persisted" >&2
    exit 5
fi
if ! activate_release_transaction "${NEW}"; then
    echo "[deploy] atomic release activation failed" >&2
    restore_mismatched_merge "${NEW}" "release activation failed" || exit 5
    exit 5
fi

if ! restart_release_consumers_transactional; then
    rollback_failed_deploy "service restart failed" || exit 5
    exit 3
fi

for ((i = 1; i <= READY_ATTEMPTS; i++)); do
    sleep "${READY_INTERVAL_SECONDS}"
    if ready_matches_release "${NEW}"; then
        complete_release_transaction || exit 5
        echo "[deploy] ready (${PREV} -> ${NEW})"
        echo "[deploy] rollback contract: ${TAG} ${PREV} ${NEW}"
        exit 0
    fi
    echo "[deploy] readiness attempt ${i}/${READY_ATTEMPTS} failed" >&2
done

rollback_failed_deploy "readiness threshold exceeded" || exit 5
exit 3

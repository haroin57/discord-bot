#!/usr/bin/env bash
# auto_deploy.sh — pull-based auto-deploy for prod and dev.
# Usage: auto_deploy.sh prod | auto_deploy.sh dev
#
# Strategy: VPS polls GitHub every 5 min via systemd timer.
# Only pulls when: (a) origin/<branch> has new commits AND
#                  (b) CI (ci.yml) reports conclusion=success for that SHA.
# No push credentials on VPS; GitHub self-hosted runner not required.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release_artifact_lib.sh
source "${SCRIPT_DIR}/release_artifact_lib.sh"

# ─── argument / config ────────────────────────────────────────────────────────
ENV="${1:-}"
if [[ "${ENV}" != "prod" && "${ENV}" != "dev" ]]; then
    echo "[auto_deploy] Usage: $0 prod | dev" >&2
    exit 1
fi

if [[ "${ENV}" == "prod" ]]; then
    BOT_DIR="${AUTO_DEPLOY_BOT_DIR:-/home/ubuntu/discord-bot}"
    BRANCH="master"
    SERVICE="${AUTO_DEPLOY_SERVICE:-discord-bot}"
    RELEASE_CONSUMERS="${AUTO_DEPLOY_CONSUMERS:-chromadb-server mcp-mem0-shared mcp-second-opinion-shared mcp-oracle-shared vi-signal-scheduler intervention-scheduler hermes-discord-codex discord-bot kanae-mcp-forward kanae-systemd-forward}"
    RELEASE_READY_PORTS="${AUTO_DEPLOY_READY_PORTS:-3004 3002}"
    RELEASE_LIVENESS_URLS="${AUTO_DEPLOY_LIVENESS_URLS:-http://127.0.0.1:8765/api/v2/heartbeat}"
else
    BOT_DIR="${AUTO_DEPLOY_BOT_DIR:-/home/ubuntu/discord-bot-dev}"
    BRANCH="dev"
    SERVICE="${AUTO_DEPLOY_SERVICE:-discord-bot-dev}"
    RELEASE_CONSUMERS="${AUTO_DEPLOY_CONSUMERS:-discord-bot-dev}"
    RELEASE_READY_PORTS="${AUTO_DEPLOY_READY_PORTS:-3003}"
    RELEASE_LIVENESS_URLS="${AUTO_DEPLOY_LIVENESS_URLS:-}"
fi

WORKFLOW="test.yml"
DEPLOY_STATE_DIR="${AUTO_DEPLOY_STATE_DIR:-/var/lib/discord-bot-deploy/${ENV}}"
LOCK_FILE="${AUTO_DEPLOY_LOCK_FILE:-${DEPLOY_STATE_DIR}/deploy.lock}"
ROLLBACK_PIN_FILE="${AUTO_DEPLOY_ROLLBACK_PIN_FILE:-${DEPLOY_STATE_DIR}/rollback.pin}"
SUCCESS_RECEIPT_FILE="${AUTO_DEPLOY_SUCCESS_RECEIPT_FILE:-${DEPLOY_STATE_DIR}/successful.sha}"
RELEASE_GATE="${AUTO_DEPLOY_RELEASE_GATE:-${BOT_DIR}/scripts/release_gate.sh}"
TRUSTED_ROLLBACK_RUNNER="${AUTO_DEPLOY_TRUSTED_ROLLBACK_RUNNER:-/usr/local/libexec/discord-bot-rollback}"
TRUSTED_ROLLBACK_RUNNER_SHA256="${AUTO_DEPLOY_TRUSTED_ROLLBACK_RUNNER_SHA256:-}"
READY_ATTEMPTS="${AUTO_DEPLOY_READY_ATTEMPTS:-36}"
READY_INTERVAL_SECONDS="${AUTO_DEPLOY_READY_INTERVAL_SECONDS:-5}"
RELEASE_ROOT="${AUTO_DEPLOY_RELEASE_ROOT:-/var/lib/discord-bot-releases/${ENV}}"
RELEASE_GENERATIONS_ROOT="${AUTO_DEPLOY_GENERATIONS_ROOT:-${RELEASE_ROOT}/generations}"
RELEASE_CURRENT_LINK="${AUTO_DEPLOY_CURRENT_LINK:-${RELEASE_ROOT}/current}"
RELEASE_BUILD_ROOT="${AUTO_DEPLOY_BUILD_ROOT:-${DEPLOY_STATE_DIR}/release-build}"
RELEASE_ACTIVATION_JOURNAL="${AUTO_DEPLOY_ACTIVATION_JOURNAL:-${DEPLOY_STATE_DIR}/activation.json}"
RELEASE_PUBLISHER="${AUTO_DEPLOY_RELEASE_PUBLISHER:-/usr/local/libexec/discord-bot-release-publisher}"
RELEASE_PUBLISHER_USE_SUDO="${AUTO_DEPLOY_PUBLISHER_USE_SUDO:-1}"
RELEASE_GENERATION_OWNER_UID="${AUTO_DEPLOY_GENERATION_OWNER_UID:-0}"
AUTO_DEPLOY_DISABLED="${AUTO_DEPLOY_DISABLED:-0}"
AUTO_DEPLOY_TEST_MODE="${AUTO_DEPLOY_TEST_MODE:-0}"
if [[ "${AUTO_DEPLOY_DISABLED}" != "0" && "${AUTO_DEPLOY_DISABLED}" != "1" ]] \
    || ! [[ "${SERVICE}" =~ ^[A-Za-z0-9@_.-]+$ ]]; then
    echo "[auto_deploy/${ENV}] invalid disable or service configuration" >&2
    exit 1
fi
# The deploy identity receives its own webhook through the root-owned
# /etc/discord-bot/credentials/deploy.env EnvironmentFile.  It must not read
# the journal-forward or application credential files.
DEPLOY_NOTIFY_WEBHOOK_URL="${DEPLOY_NOTIFY_WEBHOOK_URL:-}"

# ─── helpers ──────────────────────────────────────────────────────────────────
log() { echo "[auto_deploy/${ENV}] $*"; }

validate_deploy_identity() {
    local expected_bot expected_consumers expected_ports expected_gate expected_runner item
    if [[ "${AUTO_DEPLOY_TEST_MODE}" == "1" ]]; then
        [[ "$(realpath -m "${BOT_DIR}")" == /tmp/* \
            && "$(realpath -m "${DEPLOY_STATE_DIR}")" == /tmp/* \
            && "$(realpath -m "${RELEASE_ROOT}")" == /tmp/* \
            && "$(realpath -m "${RELEASE_BUILD_ROOT}")" == /tmp/* \
            && "${RELEASE_PUBLISHER_USE_SUDO}" == "0" \
            && "${RELEASE_GENERATION_OWNER_UID}" == "$(id -u)" \
            && "${RELEASE_TEST_PUBLISHER:-0}" == "1" ]] || return 1
        return 0
    fi
    [[ "${AUTO_DEPLOY_TEST_MODE}" == "0" \
        && "${RELEASE_PUBLISHER}" == "/usr/local/libexec/discord-bot-release-publisher" \
        && "${RELEASE_PUBLISHER_USE_SUDO}" == "1" \
        && "${RELEASE_GENERATION_OWNER_UID}" == "0" ]] || return 1
    if [[ "${ENV}" == "prod" ]]; then
        expected_bot="/home/ubuntu/discord-bot"
        expected_consumers="chromadb-server mcp-mem0-shared mcp-second-opinion-shared mcp-oracle-shared vi-signal-scheduler intervention-scheduler hermes-discord-codex discord-bot kanae-mcp-forward kanae-systemd-forward"
        expected_ports="3004 3002"
        expected_runner="/usr/local/libexec/discord-bot-rollback"
    else
        expected_bot="/home/ubuntu/discord-bot-dev"
        expected_consumers="discord-bot-dev"
        expected_ports="3003"
        expected_runner="/usr/local/libexec/discord-bot-rollback"
    fi
    expected_gate="${expected_bot}/scripts/release_gate.sh"
    [[ "${BOT_DIR}" == "${expected_bot}" \
        && "${RELEASE_GATE}" == "${expected_gate}" \
        && "${TRUSTED_ROLLBACK_RUNNER}" == "${expected_runner}" ]] || return 1
    for item in ${expected_consumers}; do
        [[ " ${RELEASE_CONSUMERS} " == *" ${item} "* ]] || return 1
    done
    [[ "$(xargs -n1 <<<"${RELEASE_CONSUMERS}" | sort -u | xargs)" \
        == "$(xargs -n1 <<<"${expected_consumers}" | sort -u | xargs)" ]] || return 1
    for item in ${expected_ports}; do
        [[ " ${RELEASE_READY_PORTS} " == *" ${item} "* ]] || return 1
    done
    [[ "$(xargs -n1 <<<"${RELEASE_READY_PORTS}" | sort -u | xargs)" \
        == "$(xargs -n1 <<<"${expected_ports}" | sort -u | xargs)" ]] || return 1
}

notify() {
    local msg="$1"
    # Mark that a specific outcome has been announced, so the EXIT catch-all
    # (on_exit) does not also fire a generic "CRASHED" message on a non-zero
    # exit that a handled branch already reported. Set even if the POST below
    # is skipped/fails: the intent is "we already spoke to Discord about this
    # run"; a generic crash line on top would just add noise (and would hit the
    # same missing-webhook condition anyway).
    NOTIFIED=1
    if [[ -z "${DEPLOY_NOTIFY_WEBHOOK_URL}" ]]; then
        log "WARN: DEPLOY_NOTIFY_WEBHOOK_URL is empty; skipping notification"
        return 0
    fi
    # Build valid JSON via python3 (already a hard dep of this script) rather
    # than shell quoting, so a message with quotes/newlines can't produce a
    # malformed payload. Call sites already tag the message with
    # "auto_deploy/<env>:", so no extra prefix is added here.
    local payload
    payload="$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' \
        "${msg}")" || return 0
    curl -fsS -X POST \
        -H "Content-Type: application/json" \
        -d "${payload}" \
        -- "${DEPLOY_NOTIFY_WEBHOOK_URL}" \
        >/dev/null 2>&1 || log "WARN: Discord notification failed (non-fatal)"
}

verify_trusted_runner() {
    local runner_real runner_digest
    [[ -x "${TRUSTED_ROLLBACK_RUNNER}" ]] || {
        log "ERROR: trusted rollback runner is missing: ${TRUSTED_ROLLBACK_RUNNER}"
        return 1
    }
    runner_real="$(realpath -e "${TRUSTED_ROLLBACK_RUNNER}")" || return 1
    case "${runner_real}" in
        "$(realpath -e "${BOT_DIR}")"/*)
            log "ERROR: trusted rollback runner must be outside the application checkout"
            return 1
            ;;
    esac
    runner_digest="$(sha256sum "${runner_real}" | awk '{print $1}')"
    if [[ -n "${TRUSTED_ROLLBACK_RUNNER_SHA256}" ]]; then
        if ! [[ "${TRUSTED_ROLLBACK_RUNNER_SHA256}" =~ ^[0-9a-f]{64}$ ]] \
            || [[ "${runner_digest}" != "${TRUSTED_ROLLBACK_RUNNER_SHA256}" ]]; then
            log "ERROR: trusted rollback runner does not match its external digest pin"
            return 1
        fi
    elif [[ "$(stat -c %u "${runner_real}")" != "0" ]] \
        || [[ -n "$(find "${runner_real}" -maxdepth 0 -perm /022 -print -quit)" ]]; then
        log "ERROR: trusted rollback runner must be root-owned and not group/other writable"
        return 1
    fi
    VERIFIED_ROLLBACK_RUNNER="${runner_real}"
    VERIFIED_ROLLBACK_RUNNER_DIGEST="${runner_digest}"
}

write_rollback_pin() {
    local tag="$1" old_sha="$2" blocked_sha="$3"
    local pin_tmp="${ROLLBACK_PIN_FILE}.tmp.$$"
    {
        printf 'target_sha=%s\n' "${old_sha}"
        printf 'tag=%s\n' "${tag}"
        printf 'blocked_sha=%s\n' "${blocked_sha}"
        printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"${pin_tmp}"
    chmod 600 "${pin_tmp}"
    mv -f "${pin_tmp}" "${ROLLBACK_PIN_FILE}"
}

log_readiness_diagnostics() {
    local expected_sha="$1" port="$2" outcome="$3" body="$4" compact
    compact="$(printf '%s' "${body}" | tr '\r\n\t' '   ')"
    log "Readiness diagnostic port=${port} expected=${expected_sha:0:12} ${outcome} body=${compact:0:4096}"
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
        log "ERROR: cannot quarantine artifacts because HEAD no longer matches ${expected_sha:0:8}"
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
        log "ERROR: artifact quarantine did not restore a clean ${expected_sha:0:8}"
        return 1
    fi
    chmod -R go-rwx "${quarantine_dir}"
    log "Quarantined failed-release artifacts outside the checkout: ${quarantine_dir}"
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
            log "Migrated legacy private checkout data: ${name}"
        fi
    done

    dirty_status="$(git status --porcelain=v1 --untracked-files=all)" || return 1
    if [[ -n "${dirty_status}" ]]; then
        printf '%s' "${dirty_status}"
        return 2
    fi
    # Ignored caches and the retired shared venv are disposable deploy artifacts.
    git clean -ffdx --quiet
    for name in .env .state .claude .venv; do
        [[ ! -e "${BOT_DIR}/${name}" && ! -L "${BOT_DIR}/${name}" ]] || return 1
    done
}

quarantine_and_clean_restore() {
    local reason="$1" blocked_sha="$2" observed_sha="${3:-}" dirty_status quarantine_dir path target
    if [[ -z "${observed_sha}" ]]; then
        observed_sha="$(git rev-parse HEAD 2>/dev/null || printf unknown)"
    fi
    dirty_status="$(git status --porcelain=v1 --untracked-files=all 2>/dev/null || true)"
    quarantine_dir="${DEPLOY_STATE_DIR}/quarantine/${blocked_sha:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
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
    write_rollback_pin "${TAG}" "${ROLLBACK_SHA}" "${blocked_sha}"
    begin_release_transaction rollback "${blocked_sha}" "${ROLLBACK_SHA}" 1 \
        || return 1
    git reset --hard "${ROLLBACK_SHA}" || return 1
    git clean -ffdx --quiet || return 1
    activate_release_transaction "${ROLLBACK_SHA}" || return 1
    if [[ "$(git rev-parse HEAD)" != "${ROLLBACK_SHA}" ]] \
        || [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
        log "ERROR: fail-closed restore did not produce clean ${ROLLBACK_SHA:0:8}"
        return 1
    fi
    chmod -R go-rwx "${quarantine_dir}"
    log "Quarantined ${observed_sha:0:8} and restored clean ${ROLLBACK_SHA:0:8}"
}

run_automatic_rollback() {
    local reason="$1"
    log "Rolling back ${NEW_SHA:0:8} to verified ${ROLLBACK_SHA:0:8} (${reason})..."
    if ! quarantine_release_artifacts "${NEW_SHA}" "${reason}"; then
        write_rollback_pin "${TAG}" "${ROLLBACK_SHA}" "${NEW_SHA}"
        notify "auto_deploy/${ENV}: ${reason} and artifact quarantine FAILED"
        return 5
    fi
    if [[ "$(sha256sum "${VERIFIED_ROLLBACK_RUNNER}" | awk '{print $1}')" \
        != "${VERIFIED_ROLLBACK_RUNNER_DIGEST}" ]]; then
        log "ERROR: trusted rollback runner changed after validation"
        notify "auto_deploy/${ENV}: ${reason} and trusted rollback runner changed"
        return 5
    fi
    write_rollback_pin "${TAG}" "${ROLLBACK_SHA}" "${NEW_SHA}"
    # The pin closes the race before releasing the deploy flock.  The trusted
    # runner then acquires the same lock itself; no caller-controlled lock
    # bypass environment exists.
    flock -u 9
    exec 9>&-
    if ! DEPLOY_BOT_DIR="${BOT_DIR}" \
        DEPLOY_SERVICE="${SERVICE}" \
        DEPLOY_CONSUMERS="${RELEASE_CONSUMERS}" \
        DEPLOY_READY_PORTS="${RELEASE_READY_PORTS}" \
        DEPLOY_LIVENESS_URLS="${RELEASE_LIVENESS_URLS}" \
        DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR}" \
        DEPLOY_LOCK_FILE="${LOCK_FILE}" \
        DEPLOY_ROLLBACK_PIN_FILE="${ROLLBACK_PIN_FILE}" \
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
        "${VERIFIED_ROLLBACK_RUNNER}" "${TAG}" "${ROLLBACK_SHA}" "${NEW_SHA}"; then
        log "ERROR: automatic rollback failed"
        notify "auto_deploy/${ENV}: ${reason} and rollback FAILED (${NEW_SHA:0:8} -> ${ROLLBACK_SHA:0:8})"
        return 5
    fi
    notify "auto_deploy/${ENV}: ${reason} — rollback pinned at ${ROLLBACK_SHA:0:8} (tag: ${TAG})"
    return 0
}

run_staging_rollback() {
    local reason="$1" restart_old="${2:-0}"
    log "Restoring staging checkout ${NEW_SHA:0:8} to ${ROLLBACK_SHA:0:8} (${reason})..."
    rm -f "${SUCCESS_RECEIPT_FILE}"
    if ! quarantine_release_artifacts "${NEW_SHA}" "${reason}"; then
        write_rollback_pin "${TAG}" "${ROLLBACK_SHA}" "${NEW_SHA}"
        notify "auto_deploy/${ENV}: ${reason} and artifact quarantine FAILED"
        return 5
    fi
    write_rollback_pin "${TAG}" "${ROLLBACK_SHA}" "${NEW_SHA}"
    begin_release_transaction rollback "${NEW_SHA}" "${ROLLBACK_SHA}" 1 \
        || return 5
    git reset --hard "${ROLLBACK_SHA}" || return 5
    git clean -ffdx --quiet || return 5
    activate_release_transaction "${ROLLBACK_SHA}" || return 5
    if [[ "${restart_old}" -eq 1 ]]; then
        if ! restart_release_consumers_transactional; then
            notify "auto_deploy/${ENV}: ${reason}; old release restart FAILED"
            return 5
        fi
        if ! ready_matches_release "${ROLLBACK_SHA}"; then
            notify "auto_deploy/${ENV}: ${reason}; old release readiness FAILED"
            return 5
        fi
    fi
    if ! ready_matches_release "${ROLLBACK_SHA}" \
        || ! complete_release_transaction; then
        notify "auto_deploy/${ENV}: ${reason}; old release transaction finalization FAILED"
        return 5
    fi
    notify "auto_deploy/${ENV}: ${reason} — staging pinned at ${ROLLBACK_SHA:0:8}"
}

# ─── crash catch-all ──────────────────────────────────────────────────────────
# set -e can kill this script on an UNGUARDED failure (git fetch / pip install /
# sudo systemctl restart), which would otherwise be SILENT: systemd_forward.py
# intentionally excludes the -autodeploy units (to avoid duplicating the notify()
# calls below), so an unhandled crash reaches nobody. The EXIT trap fires a
# generic alert for any non-zero exit that a handled branch hasn't already
# announced. notify() marks NOTIFIED=1, so a specific failure message is never
# doubled by this catch-all.
NOTIFIED=0
# shellcheck disable=SC2317  # invoked indirectly via `trap`, not dead code
on_exit() {
    local rc=$?
    if [[ "${rc}" -ne 0 && "${NOTIFIED}" -eq 0 ]]; then
        notify "auto_deploy/${ENV}: CRASHED exit=${rc} — unhandled failure (journalctl -u ${SERVICE}-autodeploy)"
    fi
}
trap on_exit EXIT

if [[ "${AUTO_DEPLOY_DISABLED}" == "1" ]]; then
    log "Automatic deployment is explicitly disabled"
    exit 0
fi
if ! validate_deploy_identity; then
    log "ERROR: deployment identity or safety allowlist is invalid"
    exit 4
fi
if ! validate_release_coordination; then
    log "ERROR: invalid consumer or readiness configuration"
    notify "auto_deploy/${ENV}: invalid consumer/readiness configuration"
    exit 4
fi

# ─── guard: BOT_DIR must exist ────────────────────────────────────────────────
if [[ ! -d "${BOT_DIR}" ]]; then
    log "ERROR: BOT_DIR ${BOT_DIR} does not exist"
    notify "auto_deploy/${ENV}: deployment checkout is not provisioned"
    exit 4
fi

cd "${BOT_DIR}"

if ! release_consumers_provisioned; then
    log "ERROR: required release consumer is not provisioned"
    notify "auto_deploy/${ENV}: required release consumer is not provisioned"
    exit 4
fi

# ─── fail-closed local gates ─────────────────────────────────────────────────
# Keep the descriptor open for the complete fetch -> mutate -> restart -> probe
# transaction. systemd normally prevents overlap for one unit, but manual runs
# and the prod/dev timers can otherwise race git and rollback state.
mkdir -p "${DEPLOY_STATE_DIR}"
chmod 700 "${DEPLOY_STATE_DIR}"
BOT_DIR_REAL="$(realpath -e "${BOT_DIR}")"
DEPLOY_STATE_REAL="$(realpath -e "${DEPLOY_STATE_DIR}")"
if [[ "${DEPLOY_STATE_REAL}" == "${BOT_DIR_REAL}" \
    || "${DEPLOY_STATE_REAL}" == "${BOT_DIR_REAL}"/* ]]; then
    log "ERROR: deploy trust state must be outside the application checkout"
    exit 4
fi
for trusted_file in "${LOCK_FILE}" "${ROLLBACK_PIN_FILE}" "${SUCCESS_RECEIPT_FILE}" \
    "${RELEASE_ACTIVATION_JOURNAL}"; do
    if [[ "$(realpath -m "$(dirname "${trusted_file}")")" != "${DEPLOY_STATE_REAL}" ]]; then
        log "ERROR: deploy lock, pin, and receipt must be direct children of trusted state"
        exit 4
    fi
done
command -v flock >/dev/null 2>&1 || {
    log "ERROR: flock is required; refusing deploy"
    exit 4
}
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    log "Another deploy or rollback owns ${LOCK_FILE}; skipping"
    exit 0
fi
release_artifact_init || exit 4

if ! [[ "${READY_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]] \
    || ! [[ "${READY_INTERVAL_SECONDS}" =~ ^[0-9]+$ ]]; then
    log "ERROR: readiness threshold must use positive attempts and a non-negative interval"
    exit 4
fi
RELEASE_RECOVERY_ATTEMPTS="${READY_ATTEMPTS}"
RELEASE_RECOVERY_INTERVAL_SECONDS="${READY_INTERVAL_SECONDS}"
if ! resume_release_transaction; then
    log "ERROR: incomplete release activation could not be recovered"
    notify "auto_deploy/${ENV}: incomplete release activation recovery failed"
    exit 5
fi

if [[ -s "${ROLLBACK_PIN_FILE}" ]]; then
    PIN_SUMMARY="$(head -1 "${ROLLBACK_PIN_FILE}" 2>/dev/null || true)"
    log "Rollback pin is active (${PIN_SUMMARY:-unreadable}); refusing automatic deploy"
    exit 0
fi

# Move legacy private data before the checkout is ever handed to the deploy UID,
# then remove ignored build artifacts. Non-ignored work remains fail-closed.
PREPARE_RC=0
DIRTY_STATUS="$(prepare_clean_checkout 2>&1)" || PREPARE_RC=$?
if [[ "${PREPARE_RC}" -ne 0 && "${PREPARE_RC}" -ne 2 ]]; then
    log "ERROR: could not sanitize the deployment checkout"
    exit 4
fi
DIRTY_STATUS="$(git status --porcelain=v1 --untracked-files=all 2>&1)" || {
    log "ERROR: cannot determine worktree status; refusing deploy"
    exit 4
}
if [[ -n "${DIRTY_STATUS}" ]]; then
    log "ERROR: worktree is dirty; refusing deploy"
    while IFS= read -r dirty_line; do
        [[ -n "${dirty_line}" ]] && log "dirty: ${dirty_line}"
    done <<<"${DIRTY_STATUS}"
    notify "auto_deploy/${ENV}: DIRTY WORKTREE — deploy refused; operator cleanup required"
    exit 4
fi

# ─── step 1: fetch & compare ──────────────────────────────────────────────────
log "Fetching origin/${BRANCH}..."
git fetch origin "${BRANCH}" --quiet

CURRENT_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse --verify 'FETCH_HEAD^{commit}')"
git cat-file -e "${REMOTE_SHA}^{commit}"

if [[ "${CURRENT_SHA}" == "${REMOTE_SHA}" ]]; then
    if release_current_matches_sha "${CURRENT_SHA}" \
        && ready_matches_release "${CURRENT_SHA}"; then
        log "HEAD and all release consumers are ready at origin/${BRANCH} (${CURRENT_SHA:0:8}); nothing to do"
        exit 0
    fi
    log "ERROR: HEAD equals origin/${BRANCH}, but the active generation or consumers do not match"
    notify "auto_deploy/${ENV}: active generation mismatch at ${CURRENT_SHA:0:8}; operator recovery required"
    exit 5
fi

# merge-based guard (replaces relying on the SHA-inequality check alone).
# The bare `CURRENT_SHA != REMOTE_SHA` test above is not sufficient to decide
# "there is something to deploy": if HEAD is at or AHEAD of origin/${BRANCH}
# (e.g. the prod checkout was left on a local/feature branch, or a commit was
# made directly in the prod dir), the SHAs differ yet the ff-only merge in
# step 4 is a guaranteed no-op. The old code fell through, then step 6
# unconditionally `systemctl restart`ed the service — every 5 min, for zero
# code delta: a restart storm that also spammed pre-deploy-* tags. Decide by
# ancestry instead of raw inequality.
#
#   git merge-base --is-ancestor A B  →  exit 0 iff A is an ancestor of B.
#
# (a) origin is already an ancestor of HEAD → HEAD contains origin, ff is a
#     no-op → nothing to deploy.
if git merge-base --is-ancestor "${REMOTE_SHA}" HEAD; then
    log "HEAD (${CURRENT_SHA:0:8}) already contains origin/${BRANCH} (${REMOTE_SHA:0:8}); local is at/ahead of origin, ff-only would be a no-op — nothing to deploy"
    exit 0
fi

# (b) HEAD is not an ancestor of origin either → the two have diverged and are
#     not fast-forwardable. Auto-restarting on this is wrong; the ff-only merge
#     in step 4 would abort anyway. Surface it once and skip instead of looping.
if ! git merge-base --is-ancestor HEAD "${REMOTE_SHA}"; then
    log "HEAD (${CURRENT_SHA:0:8}) has diverged from origin/${BRANCH} (${REMOTE_SHA:0:8}); not fast-forwardable — skipping (prod checkout needs manual attention)"
    notify "auto_deploy/${ENV}: HEAD diverged from origin/${BRANCH} (${CURRENT_SHA:0:8} vs ${REMOTE_SHA:0:8}) — deploy skipped, checkout needs attention"
    exit 0
fi

# Only here is origin/${BRANCH} genuinely a fast-forward ahead of HEAD.
log "New commits available: ${CURRENT_SHA:0:8} -> ${REMOTE_SHA:0:8}"

# ─── step 2: CI green gate ────────────────────────────────────────────────────
log "Checking CI status for ${REMOTE_SHA:0:8} (${WORKFLOW})..."

CI_JSON="$(gh run list \
    --repo "$(git remote get-url origin | sed 's|https://github.com/||;s|\.git$||')" \
    --branch "${BRANCH}" \
    --workflow "${WORKFLOW}" \
    --limit 5 \
    --json headSha,conclusion,status 2>&1)" || {
    log "WARN: gh run list failed (gh not configured or network error); skipping deploy"
    exit 0
}

# Find a run for the exact target SHA
CI_CONCLUSION="$(echo "${CI_JSON}" | python3 -c "
import json, sys
runs = json.load(sys.stdin)
target = '${REMOTE_SHA}'
for r in runs:
    if r.get('headSha', '') == target:
        print(r.get('conclusion',''))
        sys.exit(0)
print('NOT_FOUND')
" 2>/dev/null || echo "PARSE_ERROR")"

case "${CI_CONCLUSION}" in
    success)
        log "CI green for ${REMOTE_SHA:0:8}; proceeding with deploy"
        ;;
    NOT_FOUND)
        log "No CI run found for ${REMOTE_SHA:0:8}; waiting for CI to run"
        exit 0
        ;;
    "")
        log "CI run in progress (no conclusion yet) for ${REMOTE_SHA:0:8}; waiting"
        exit 0
        ;;
    PARSE_ERROR)
        log "WARN: failed to parse gh output; skipping deploy"
        exit 0
        ;;
    *)
        log "CI status '${CI_CONCLUSION}' for ${REMOTE_SHA:0:8}; not deploying (failure or skipped)"
        notify "auto_deploy/${ENV}: CI=${CI_CONCLUSION} on ${REMOTE_SHA:0:8} — deploy skipped"
        exit 0
        ;;
esac

# ─── step 3: release gate ─────────────────────────────────────────────────────
# Dev proves the target with the local smoke suite and publishes a success
# receipt after /ready passes. Prod requires that staging evidence and performs
# a snapshot + restore rehearsal before mutating HEAD.
if [[ ! -x "${RELEASE_GATE}" ]]; then
    log "ERROR: release gate is missing or not executable: ${RELEASE_GATE}"
    exit 4
fi
if [[ "${ENV}" == "prod" ]]; then
    if ! verify_trusted_runner; then
        notify "auto_deploy/${ENV}: trusted rollback runner validation failed"
        exit 4
    fi
    if ! "${RELEASE_GATE}" prod "${REMOTE_SHA}"; then
        log "ERROR: release gate failed for ${REMOTE_SHA:0:8}; refusing deploy"
        notify "auto_deploy/${ENV}: RELEASE GATE FAILED on ${REMOTE_SHA:0:8} — deploy refused"
        exit 4
    fi
fi

if ! build_release_venv "${CURRENT_SHA}" || ! build_release_venv "${REMOTE_SHA}"; then
    log "ERROR: SHA-specific release environment validation failed"
    notify "auto_deploy/${ENV}: release environment build failed for ${REMOTE_SHA:0:8}"
    exit 4
fi

# The release gate may run for several minutes. Re-check both mutable safety
# inputs while the flock is still held before changing HEAD.
if [[ -s "${ROLLBACK_PIN_FILE}" ]] \
    || [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
    log "ERROR: rollback pin or dirty state appeared during release gate; refusing deploy"
    exit 4
fi

# ─── step 4: pre-deploy tag (prod only) ───────────────────────────────────────
ROLLBACK_SHA="${CURRENT_SHA}"
TAG="pre-deploy-${ENV}-$(date +%Y%m%d-%H%M%S)-${CURRENT_SHA:0:8}"
git tag "${TAG}" "${CURRENT_SHA}"
log "Tagged rollback point: ${TAG}"

# ─── step 5: fast-forward merge ───────────────────────────────────────────────
log "Merging fetched commit ${REMOTE_SHA:0:8}..."
git merge --ff-only "${REMOTE_SHA}" || {
    log "ERROR: fast-forward failed; aborting (HEAD still at ${ROLLBACK_SHA:0:8})"
    notify "auto_deploy/${ENV}: ff-only merge failed for ${REMOTE_SHA:0:8}"
    exit 2
}
NEW_SHA="$(git rev-parse HEAD)"
log "Merged: ${ROLLBACK_SHA:0:8} -> ${NEW_SHA:0:8}"

if [[ "${NEW_SHA}" != "${REMOTE_SHA}" ]]; then
    log "ERROR: merged SHA does not match gated fetched commit"
    if ! quarantine_and_clean_restore \
        "POST-MERGE SHA MISMATCH" "${REMOTE_SHA}" "${NEW_SHA}"; then
        notify "auto_deploy/${ENV}: POST-MERGE SHA MISMATCH cleanup FAILED; pin active"
        exit 5
    fi
    notify "auto_deploy/${ENV}: POST-MERGE SHA MISMATCH; quarantined and pinned"
    exit 5
fi

# Belt-and-suspenders: after the merge-base guard in step 1 this can't happen,
# but never restart the service for a deploy that didn't move HEAD. A zero-delta
# restart is exactly the storm we're preventing; guard the restart on it too.
if [[ "${NEW_SHA}" == "${ROLLBACK_SHA}" ]]; then
    log "HEAD unchanged after merge (${NEW_SHA:0:8}); skipping restart — no-op deploy"
    exit 0
fi

# Dev is the staging environment. Run the smoke suite against the newly merged
# tree before restart; only a subsequent successful /ready probe publishes the
# receipt consumed by the production release gate.
if [[ "${ENV}" == "dev" ]] && ! RELEASE_GATE_VENV="$(release_venv_path "${NEW_SHA}")" \
    "${RELEASE_GATE}" dev "${NEW_SHA}"; then
    log "ERROR: staging smoke failed on ${NEW_SHA:0:8}; service was not restarted"
    run_staging_rollback "STAGING SMOKE FAILED" || exit 5
    exit 4
fi

# The service identity has no write access to the checkout.  Recheck that
# contract immediately before restart, after dependency hooks and staging
# smoke have run, and verify the target/tag identities again.
if [[ -s "${ROLLBACK_PIN_FILE}" ]] \
    || [[ "$(git rev-parse HEAD)" != "${NEW_SHA}" ]] \
    || [[ "$(git rev-parse --verify "refs/tags/${TAG}^{commit}")" != "${ROLLBACK_SHA}" ]]; then
    log "ERROR: repository, rollback tag, or pin changed immediately before restart"
    if [[ ! -s "${ROLLBACK_PIN_FILE}" ]]; then
        if ! quarantine_and_clean_restore "PRE-RESTART INTEGRITY FAILED" "${NEW_SHA}" \
            "$(git rev-parse HEAD 2>/dev/null || printf unknown)"; then
            notify "auto_deploy/${ENV}: PRE-RESTART cleanup FAILED; pin active"
            exit 5
        fi
    fi
    notify "auto_deploy/${ENV}: PRE-RESTART INTEGRITY FAILED on ${NEW_SHA:0:8}"
    exit 5
fi
if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
    log "ERROR: build or dependency step dirtied the merged checkout"
    quarantine_and_clean_restore "PRE-RESTART DIRTY WORKTREE" "${NEW_SHA}" || exit 5
    exit 5
fi
for private_name in .env .state .claude .venv; do
    if [[ -e "${BOT_DIR}/${private_name}" || -L "${BOT_DIR}/${private_name}" ]]; then
        log "ERROR: merged release contains forbidden private path ${private_name}"
        if ! quarantine_and_clean_restore \
            "FORBIDDEN PRIVATE CHECKOUT PATH" "${NEW_SHA}"; then
            notify "auto_deploy/${ENV}: private-path cleanup FAILED; pin active"
            exit 5
        fi
        notify "auto_deploy/${ENV}: forbidden private checkout path; target pinned"
        exit 5
    fi
done

if ! begin_release_transaction deploy "${ROLLBACK_SHA}" "${NEW_SHA}"; then
    log "ERROR: release activation transaction could not be persisted"
    exit 5
fi
if ! activate_release_transaction "${NEW_SHA}"; then
    log "ERROR: atomic release activation failed before service start"
    if ! quarantine_and_clean_restore "RELEASE ACTIVATION FAILED" "${NEW_SHA}"; then
        notify "auto_deploy/${ENV}: release activation cleanup FAILED; pin active"
        exit 5
    fi
    notify "auto_deploy/${ENV}: release activation failed and target was pinned"
    exit 5
fi

# ─── step 7: restart service ──────────────────────────────────────────────────
if ! restart_release_consumers_transactional; then
    if [[ "${ENV}" == "prod" ]]; then
        run_automatic_rollback "RESTART FAILED" || exit 5
    else
        run_staging_rollback "RESTART FAILED" 1 || exit 5
    fi
    exit 5
fi

# ─── step 8: liveness/readiness check ─────────────────────────────────────────
READY_OK=0
for ((i = 1; i <= READY_ATTEMPTS; i++)); do
    sleep "${READY_INTERVAL_SECONDS}"
    if ready_matches_release "${NEW_SHA}"; then
        log "Ready OK (attempt ${i}/${READY_ATTEMPTS})"
        READY_OK=1
        break
    fi
    if curl -fsS "http://127.0.0.1:${RELEASE_READY_PORTS%% *}/live" >/dev/null 2>&1; then
        log "Ready attempt ${i}/${READY_ATTEMPTS} failed; process is live"
    else
        log "Ready attempt ${i}/${READY_ATTEMPTS} failed; process is not live"
    fi
done

if [[ "${READY_OK}" -eq 1 ]]; then
    receipt_tmp="${SUCCESS_RECEIPT_FILE}.tmp.$$"
    printf '%s\n' "${NEW_SHA}" >"${receipt_tmp}"
    chmod 600 "${receipt_tmp}"
    mv -f "${receipt_tmp}" "${SUCCESS_RECEIPT_FILE}"
    complete_release_transaction || {
        log "ERROR: deploy succeeded but activation journal could not be finalized"
        exit 5
    }
    log "Deploy succeeded: ${ROLLBACK_SHA:0:8} -> ${NEW_SHA:0:8}"
    notify "auto_deploy/${ENV}: deployed ${ROLLBACK_SHA:0:8} -> ${NEW_SHA:0:8} OK"
    exit 0
fi

# ─── step 9: readiness threshold exceeded ─────────────────────────────────────
log "READINESS CHECK FAILED after ${READY_ATTEMPTS} attempts on ${NEW_SHA:0:8}"

if [[ "${ENV}" == "prod" ]]; then
    if ! run_automatic_rollback "READINESS FAILED"; then
        exit 5
    fi
else
    run_staging_rollback "READINESS FAILED" 1 || exit 5
fi

exit 3

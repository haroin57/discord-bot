#!/usr/bin/env bash
# auto_deploy.sh — pull-based auto-deploy for prod and dev.
# Usage: auto_deploy.sh prod | auto_deploy.sh dev
#
# Strategy: VPS polls GitHub every 5 min via systemd timer.
# Only pulls when: (a) origin/<branch> has new commits AND
#                  (b) CI (ci.yml) reports conclusion=success for that SHA.
# No push credentials on VPS; GitHub self-hosted runner not required.
set -euo pipefail

# ─── argument / config ────────────────────────────────────────────────────────
ENV="${1:-}"
if [[ "${ENV}" != "prod" && "${ENV}" != "dev" ]]; then
    echo "[auto_deploy] Usage: $0 prod | dev" >&2
    exit 1
fi

if [[ "${ENV}" == "prod" ]]; then
    BOT_DIR="/home/ubuntu/discord-bot"
    BRANCH="master"
    SERVICE="discord-bot"
    PORT="3002"
else
    BOT_DIR="/home/ubuntu/discord-bot-dev"
    BRANCH="dev"
    SERVICE="discord-bot-dev"
    PORT="3003"
fi

WORKFLOW="test.yml"
# Deploy notifications reuse the durable journal-forward Discord webhook
# (${BOT_DIR}/.env.journal-forward:JOURNAL_WEBHOOK_URL) instead of the old
# /tmp/kanae-notify-token.txt worker token, which lived on tmpfs and was
# silently lost on every reboot — leaving deploys unannounced. The webhook
# file survives reboots and is already in active use by
# discord-bot-journal-forward.service, so notifications are durable now.
NOTIFY_ENV_FILE="${BOT_DIR}/.env.journal-forward"

# ─── helpers ──────────────────────────────────────────────────────────────────
log() { echo "[auto_deploy/${ENV}] $*"; }

notify() {
    local msg="$1"
    # Mark that a specific outcome has been announced, so the EXIT catch-all
    # (on_exit) does not also fire a generic "CRASHED" message on a non-zero
    # exit that a handled branch already reported. Set even if the POST below
    # is skipped/fails: the intent is "we already spoke to Discord about this
    # run"; a generic crash line on top would just add noise (and would hit the
    # same missing-webhook condition anyway).
    NOTIFIED=1
    if [[ ! -f "${NOTIFY_ENV_FILE}" ]]; then
        log "WARN: ${NOTIFY_ENV_FILE} not found; skipping Discord notification"
        return 0
    fi
    local webhook
    webhook="$(grep -E '^JOURNAL_WEBHOOK_URL=' "${NOTIFY_ENV_FILE}" | head -1 | cut -d= -f2-)"
    if [[ -z "${webhook}" ]]; then
        log "WARN: JOURNAL_WEBHOOK_URL empty in ${NOTIFY_ENV_FILE}; skipping notification"
        return 0
    fi
    # Build valid JSON via python3 (already a hard dep of this script) rather
    # than shell quoting, so a message with quotes/newlines can't produce a
    # malformed payload. Call sites already tag the message with
    # "auto_deploy/<env>:", so no extra prefix is added here.
    local payload
    payload="$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' \
        "${msg}")" || return 0
    curl -fsS -X POST "${webhook}" \
        -H "Content-Type: application/json" \
        -d "${payload}" \
        >/dev/null 2>&1 || log "WARN: Discord notification failed (non-fatal)"
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

# ─── dev: graceful skip if service doesn't exist yet ─────────────────────────
# unit ファイルの存在で判定する。list-units + grep は先頭マーカー(●)や
# 同名 prefix (discord-bot-dev-autodeploy) の混入で誤爆するため使わない。
if [[ "${ENV}" == "dev" ]]; then
    if ! systemctl cat "${SERVICE}.service" >/dev/null 2>&1; then
        log "Service ${SERVICE}.service not found (dev env not provisioned yet); skipping"
        exit 0
    fi
fi

# ─── guard: BOT_DIR must exist ────────────────────────────────────────────────
if [[ ! -d "${BOT_DIR}" ]]; then
    log "BOT_DIR ${BOT_DIR} does not exist; skipping"
    exit 0
fi

cd "${BOT_DIR}"

# ─── step 1: fetch & compare ──────────────────────────────────────────────────
log "Fetching origin/${BRANCH}..."
git fetch origin "${BRANCH}" --quiet

CURRENT_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "origin/${BRANCH}")"

if [[ "${CURRENT_SHA}" == "${REMOTE_SHA}" ]]; then
    log "HEAD already at origin/${BRANCH} (${CURRENT_SHA:0:8}); nothing to do"
    exit 0
fi

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
    if r.get('headSha','').startswith(target) or target.startswith(r.get('headSha','')):
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

# ─── step 3: pre-deploy tag (prod only) ───────────────────────────────────────
ROLLBACK_SHA="${CURRENT_SHA}"
if [[ "${ENV}" == "prod" ]]; then
    TAG="pre-deploy-$(date +%Y%m%d-%H%M%S)"
    git tag "${TAG}"
    log "Tagged rollback point: ${TAG}"
fi

# ─── step 4: fast-forward merge ───────────────────────────────────────────────
log "Merging origin/${BRANCH}..."
git merge --ff-only "origin/${BRANCH}" || {
    log "ERROR: fast-forward failed; aborting (HEAD still at ${ROLLBACK_SHA:0:8})"
    notify "auto_deploy/${ENV}: ff-only merge failed for ${REMOTE_SHA:0:8}"
    exit 2
}
NEW_SHA="$(git rev-parse HEAD)"
log "Merged: ${ROLLBACK_SHA:0:8} -> ${NEW_SHA:0:8}"

# ─── step 5: reinstall deps if lock changed ───────────────────────────────────
if git diff --name-only "${ROLLBACK_SHA}" "${NEW_SHA}" | grep -q "requirements\.lock$"; then
    log "requirements.lock changed; reinstalling deps"
    "${BOT_DIR}/.venv/bin/pip" install -r "${BOT_DIR}/requirements.lock" --quiet
fi

# ─── step 6: restart service ──────────────────────────────────────────────────
log "Restarting ${SERVICE}..."
sudo systemctl restart "${SERVICE}"

# ─── step 7: health check ─────────────────────────────────────────────────────
HEALTH_OK=0
for i in 1 2 3 4 5 6; do
    sleep 5
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        log "Health OK (attempt ${i})"
        HEALTH_OK=1
        break
    fi
    log "Health attempt ${i}/6 failed..."
done

if [[ "${HEALTH_OK}" -eq 1 ]]; then
    log "Deploy succeeded: ${ROLLBACK_SHA:0:8} -> ${NEW_SHA:0:8}"
    notify "auto_deploy/${ENV}: deployed ${ROLLBACK_SHA:0:8} -> ${NEW_SHA:0:8} OK"
    exit 0
fi

# ─── step 8: health failed ────────────────────────────────────────────────────
log "HEALTH CHECK FAILED after deploy to ${NEW_SHA:0:8}"

if [[ "${ENV}" == "prod" ]]; then
    log "Rolling back to ${ROLLBACK_SHA:0:8}..."
    # Use bot-rollback.sh if available, else fall back to manual SHA reset
    ROLLBACK_SCRIPT="${BOT_DIR}/scripts/bot-rollback.sh"
    if [[ -x "${ROLLBACK_SCRIPT}" ]]; then
        # bot-rollback.sh expects a tag name; use the pre-deploy tag we created
        "${ROLLBACK_SCRIPT}" "${TAG}" || true
    else
        # Fallback: reset to pre-deploy SHA using git checkout of the file tree
        # We intentionally avoid git reset --hard. Instead, re-checkout via
        # an explicit ff to the old SHA by writing + applying a temp branch.
        log "bot-rollback.sh not found; attempting manual rollback via git checkout <sha>"
        git checkout "${ROLLBACK_SHA}" -- . || true
        git checkout "${ROLLBACK_SHA}" || true
        sudo systemctl restart "${SERVICE}" || true
    fi
    notify "auto_deploy/${ENV}: HEALTH FAILED — rolled back to ${ROLLBACK_SHA:0:8} (tag: ${TAG})"
else
    # dev: don't rollback, just alert
    notify "auto_deploy/${ENV}: HEALTH FAILED on ${NEW_SHA:0:8} — dev branch broken, no rollback"
    log "dev: no rollback performed"
fi

exit 3

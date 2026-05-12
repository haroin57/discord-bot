#!/usr/bin/env bash
# SRE: deploy current master to the running bot.
# Tags pre-deploy commit, fetches latest, runs migrations (none yet), restarts.
set -euo pipefail

BOT_DIR="/home/ubuntu/discord-bot"
cd "${BOT_DIR}"

# 1. Tag the current HEAD as a rollback point
TAG="pre-deploy-$(date +%Y%m%d-%H%M%S)"
git tag "${TAG}"
echo "[deploy] tagged current HEAD as ${TAG}"

# 2. Fetch & fast-forward
git fetch --tags origin
PREV="$(git rev-parse HEAD)"
git merge --ff-only origin/master || {
    echo "[deploy] fast-forward failed; aborting (HEAD at ${PREV})"
    exit 2
}
NEW="$(git rev-parse HEAD)"

if [[ "${PREV}" == "${NEW}" ]]; then
    echo "[deploy] no changes; skipping restart"
    exit 0
fi

# 3. Install dependencies if the lock changed
if git diff --name-only "${PREV}" "${NEW}" | grep -q "requirements\.lock$"; then
    echo "[deploy] requirements.lock changed; reinstalling deps"
    "${BOT_DIR}/.venv/bin/pip" install -r requirements.lock
fi

# 4. Restart the bot via systemd (graceful, SIGTERM-aware after C1 fix)
echo "[deploy] restarting discord-bot.service"
sudo systemctl restart discord-bot

# 5. Smoke check: /health within 30s
for i in 1 2 3 4 5 6; do
    sleep 5
    if curl -fsS http://127.0.0.1:3002/health >/dev/null 2>&1; then
        echo "[deploy] health OK (attempt ${i})"
        echo "[deploy] deployed ${PREV} -> ${NEW}. Rollback: ./scripts/bot-rollback.sh ${TAG}"
        exit 0
    fi
done
echo "[deploy] HEALTH CHECK FAILED — consider rollback: ./scripts/bot-rollback.sh ${TAG}"
exit 3

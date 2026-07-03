#!/usr/bin/env bash
# SRE: rollback to a previous pre-deploy tag.
set -euo pipefail
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <tag-name>"
    echo "Available tags:"
    git tag -l "pre-deploy-*" | tail -5
    git tag -l "refactor-*"
    exit 1
fi
TAG="$1"
BOT_DIR="/home/ubuntu/discord-bot"
cd "${BOT_DIR}"
git rev-parse "refs/tags/${TAG}" >/dev/null || { echo "Tag not found: ${TAG}"; exit 2; }
echo "[rollback] reverting to ${TAG}"
# Detached HEAD is intentional — operator should investigate before re-attaching to master
git checkout "refs/tags/${TAG}"
sudo systemctl restart discord-bot
echo "[rollback] done. HEAD is now at ${TAG} (detached). Investigate the bad commit on a branch."

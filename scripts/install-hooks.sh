#!/usr/bin/env bash
# Symlink .git-hooks/* into .git/hooks/ so they run.
set -euo pipefail
HOOKS_DIR="$(git rev-parse --show-toplevel)/.git-hooks"
TARGET_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "${TARGET_DIR}"
for hook in "${HOOKS_DIR}"/*; do
    name="$(basename "${hook}")"
    ln -sf "${hook}" "${TARGET_DIR}/${name}"
    chmod +x "${hook}"
    echo "installed hook: ${name}"
done

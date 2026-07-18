#!/usr/bin/env bash
# Unprivileged release-gate client for the fixed root snapshot helper.
set -euo pipefail
exec /usr/bin/sudo -n -- /usr/local/libexec/discord-bot-snapshot "$@"

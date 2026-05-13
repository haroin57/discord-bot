#!/usr/bin/env bash
# Thin cron wrapper — emotion-driven logic lives in kanae_emotion_tick.py.
# Kept as a bash file so the existing crontab entry remains valid.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../.venv/bin/python" "${SCRIPT_DIR}/kanae_emotion_tick.py" "$@"

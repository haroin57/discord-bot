#!/usr/bin/env bash
# mmdc_safe.sh — Mermaid CLI wrapper that cannot leak headless Chrome.
#
# Usage: scripts/mmdc_safe.sh -i input.mmd -o /tmp/mermaid_output/out.png [mmdc options]
#
# - Resolves the newest Chrome in the puppeteer cache. mermaid-cli pins an
#   older build that is usually not installed, so plain `mmdc` fails.
# - config/puppeteer-mmdc.json uses the CDP pipe transport, so Chrome exits
#   by itself when mmdc dies, timeout kills included.
# - Hard timeout (MMDC_TIMEOUT seconds, default 60) with SIGKILL fallback.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config="${MMDC_PUPPETEER_CONFIG:-$script_dir/../config/puppeteer-mmdc.json}"

if [[ -z "${PUPPETEER_EXECUTABLE_PATH:-}" ]]; then
    chrome="$(ls -d "$HOME"/.cache/puppeteer/chrome/*/chrome-linux64/chrome 2>/dev/null | sort -V | tail -n 1 || true)"
    if [[ -n "$chrome" ]]; then
        export PUPPETEER_EXECUTABLE_PATH="$chrome"
    fi
fi

exec timeout --kill-after=5 "${MMDC_TIMEOUT:-60}" mmdc -p "$config" "$@"

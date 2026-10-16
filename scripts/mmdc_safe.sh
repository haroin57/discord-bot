#!/usr/bin/env bash
# mmdc_safe.sh — Mermaid CLI wrapper that cannot leak headless Chrome.
# Installed as /usr/local/bin/mmdc-safe by scripts/apply_memory_diet_host.sh.
#
# Usage: mmdc-safe -i input.mmd -o /tmp/mermaid_output/out.png [mmdc options]
#
# - Chrome: $PUPPETEER_EXECUTABLE_PATH, then the newest build in
#   $HOME/.cache/puppeteer, then the system Chrome. mermaid-cli pins a build
#   that is usually not installed, and users like kanae-bot have no puppeteer
#   cache, so plain `mmdc` fails with "Could not find Chrome".
# - The generated puppeteer config uses the CDP pipe transport, so Chrome
#   exits by itself when mmdc dies (timeout and SIGKILL included).
# - Hard timeout: MMDC_TIMEOUT seconds (default 60), with a SIGKILL fallback.
set -euo pipefail

chrome="${PUPPETEER_EXECUTABLE_PATH:-}"
if [[ -z "$chrome" ]]; then
    chrome="$(ls -d "${HOME:-/nonexistent}"/.cache/puppeteer/chrome/*/chrome-linux64/chrome 2>/dev/null | sort -V | tail -n 1 || true)"
fi
if [[ -z "$chrome" ]]; then
    for candidate in /usr/bin/google-chrome /usr/bin/google-chrome-stable /usr/bin/chromium /usr/bin/chromium-browser; do
        if [[ -x "$candidate" ]]; then chrome="$candidate"; break; fi
    done
fi
if [[ -z "$chrome" || ! -x "$chrome" ]]; then
    echo "mmdc-safe: no usable Chrome found (set PUPPETEER_EXECUTABLE_PATH)" >&2
    exit 127
fi
export PUPPETEER_EXECUTABLE_PATH="$chrome"

config="$(mktemp "${TMPDIR:-/tmp}/mmdc-puppeteer.XXXXXX.json")"
trap 'rm -f -- "$config"' EXIT
cat > "$config" <<'JSON'
{
  "headless": "new",
  "pipe": true,
  "args": [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-component-update",
    "--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,OptimizationTargetPrediction,OnDeviceModelService"
  ]
}
JSON

set +e
timeout --kill-after=5 "${MMDC_TIMEOUT:-60}" mmdc -p "$config" "$@"
rc=$?
set -e
exit "$rc"

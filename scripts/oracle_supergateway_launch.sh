#!/usr/bin/env bash
# Start the shared Oracle supergateway only after the repository patch set has
# been applied. The patch is fail-closed because it owns the loopback bind.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_BIN="${ORACLE_NODE_BIN:-/home/ubuntu/.volta/tools/image/node/24.14.1/bin/node}"
SUPERGATEWAY_ROOT="${ORACLE_SUPERGATEWAY_ROOT:-/home/ubuntu/.volta/tools/image/node/24.14.1/lib/node_modules/supergateway}"
SUPERGATEWAY_ENTRY="${ORACLE_SUPERGATEWAY_ENTRY:-${SUPERGATEWAY_ROOT}/dist/index.js}"
GATEWAY_IMPL="${SUPERGATEWAY_ROOT}/dist/gateways/stdioToStatelessStreamableHttp.js"
PORT="${ORACLE_MCP_PORT:-8015}"

[[ "${PORT}" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || {
    echo "[oracle-supergateway] invalid port: ${PORT}" >&2
    exit 2
}
[[ -x "${NODE_BIN}" && -f "${SUPERGATEWAY_ENTRY}" ]] || {
    echo "[oracle-supergateway] node or supergateway installation is missing" >&2
    exit 3
}

"${SCRIPT_DIR}/supergateway_apply_patches.sh"
if ! grep -Fq "app.listen(port, '127.0.0.1'" "${GATEWAY_IMPL}"; then
    echo "[oracle-supergateway] loopback bind patch is absent; refusing listener startup" >&2
    exit 4
fi

exec "${NODE_BIN}" "${SUPERGATEWAY_ENTRY}" \
    --stdio "${SCRIPT_DIR}/oracle_mcp_launch.sh" \
    --port "${PORT}" \
    --outputTransport streamableHttp \
    --streamableHttpPath /mcp

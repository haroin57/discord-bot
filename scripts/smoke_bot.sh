#!/usr/bin/env bash
# P0.25-b (docs/2026-07-03-grand-refactor-plan.md): pre-restart smoke check.
# No staging bot token exists yet, so this validates everything short of the
# Discord connection: entry-point imports, config parsing, and a focused
# runtime test subset. Lint and type checks run in CI and are intentionally not
# installed into the immutable runtime environment. Run before every restart
# during the refactor train; a red result here means DO NOT restart.
#
# Exit 0 = safe to restart. Non-zero = investigate first.
set -euo pipefail

BOT_DIR="${BOT_DIR:-/home/ubuntu/discord-bot}"
BOT_VENV="${BOT_VENV:?BOT_VENV must point to the SHA-specific external venv}"
PY="${BOT_VENV}/bin/python"
cd "${BOT_DIR}"

export PYTHONDONTWRITEBYTECODE=1

echo "[smoke] 1/2 entry-point imports (bot, health_server, main_async, MCP servers)"
"${PY}" - <<'EOF'
import importlib
for mod in (
    "config", "bot", "services.health_server", "main_async",
    "mcp_mem0_server", "second_opinion_mcp_server",
    "services.claude_api.entry", "services.mem0.search",
):
    importlib.import_module(mod)
    print(f"  - {mod} OK")
EOF

echo "[smoke] 2/2 fast test subset (session/state/config surface)"
"${PY}" -m pytest -q -x \
    -p no:cacheprovider \
    tests/test_config.py tests/test_session.py tests/test_atomic_write.py \
    2>&1

echo "[smoke] ALL GREEN — safe to restart"

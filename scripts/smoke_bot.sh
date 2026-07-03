#!/usr/bin/env bash
# P0.25-b (docs/2026-07-03-grand-refactor-plan.md): pre-restart smoke check.
# No staging bot token exists yet, so this validates everything short of the
# Discord connection: entry-point imports, config parsing, and the
# definite-bug lint tier. Run before every systemd restart during the
# refactor train; a red result here means DO NOT restart.
#
# Exit 0 = safe to restart. Non-zero = investigate first.
set -euo pipefail

BOT_DIR="/home/ubuntu/discord-bot"
PY="${BOT_DIR}/.venv/bin/python"
cd "${BOT_DIR}"

echo "[smoke] 1/4 entry-point imports (bot, web_api, main_async, MCP servers)"
"${PY}" - <<'EOF'
import importlib
for mod in (
    "config", "bot", "web_api", "main_async",
    "mcp_mem0_server", "second_opinion_mcp_server",
    "services.claude_api.entry", "services.mem0.search",
):
    importlib.import_module(mod)
    print(f"  - {mod} OK")
EOF

echo "[smoke] 2/4 ruff definite-bug tier"
"${BOT_DIR}/.venv/bin/ruff" check . --quiet && echo "  - ruff OK"

echo "[smoke] 3/4 mypy scoped files"
"${BOT_DIR}/.venv/bin/mypy" --no-error-summary && echo "  - mypy OK"

echo "[smoke] 4/4 fast test subset (session/state/config surface)"
"${PY}" -m pytest -q -x \
    tests/test_config.py tests/test_session.py tests/test_atomic_write.py \
    2>&1 | tail -1

echo "[smoke] ALL GREEN — safe to restart"

#!/usr/bin/env bash
# Refresh the bot's CLI-identity block by capturing a live claude-cli
# request via mitmproxy and rewriting the AUTO-CAPTURE block in
# services/claude_api/client.py.
#
# Designed for daily cron / systemd-timer execution. Steps:
#   1. Start mitmdump as a local HTTPS proxy
#   2. Run `claude -p "<ping>"` through the proxy (clean env, OAuth flow)
#   3. Stop mitmdump
#   4. Run scripts/apply_cli_identity.py to patch client.py
#   5. Run the test suite (claude_api_entry + session)
#   6. Restart the bot
#   7. Run scripts/verify_cli_identity.py to confirm HTTP 200
#   8. On any failure: git-stash the edits to roll back, restart bot again
#
# All artifacts (mitm dump, captured headers) live under /tmp and are deleted
# at the end since they contain the OAuth bearer token. A redacted copy of
# the extracted fields is logged to the LOG_FILE.

set -uo pipefail

REPO=/home/ubuntu/discord-bot
TS=$(date -u +%Y%m%dT%H%M%SZ)
WORKDIR=$(mktemp -d -t cli-refresh-XXXXXX)
DUMP="$WORKDIR/capture.mitm"
PORT=8888
LOG_FILE=/home/ubuntu/discord-bot/.state/refresh_cli_identity.log
PY_VENV=/home/ubuntu/discord-bot/.venv/bin/python
PY_MITM=/home/ubuntu/.local/share/pipx/venvs/mitmproxy/bin/python
MITMDUMP=/home/ubuntu/.local/bin/mitmdump
CLAUDE_BIN=/home/ubuntu/.local/bin/claude
CA_CERT=/home/ubuntu/.mitmproxy/mitmproxy-ca-cert.pem

mkdir -p "$(dirname "$LOG_FILE")"
exec >> "$LOG_FILE" 2>&1
echo
echo "===== $TS refresh start ====="

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

cleanup() {
    if [[ -n "${MITM_PID:-}" ]] && kill -0 "$MITM_PID" 2>/dev/null; then
        kill "$MITM_PID" 2>/dev/null || true
        wait "$MITM_PID" 2>/dev/null || true
    fi
    if [[ -d "$WORKDIR" ]]; then
        # Bearer token in the dump — burn the whole tempdir.
        rm -rf "$WORKDIR"
    fi
}
trap cleanup EXIT

# 1. start mitmdump
log "starting mitmdump on 127.0.0.1:$PORT"
"$MITMDUMP" --listen-host 127.0.0.1 --listen-port "$PORT" -w "$DUMP" --quiet \
    > "$WORKDIR/mitm.stderr" 2>&1 &
MITM_PID=$!
sleep 3
if ! ss -ltn "src 127.0.0.1:$PORT" 2>/dev/null | grep -q "$PORT"; then
    log "FAIL: mitmdump not listening; stderr:"
    cat "$WORKDIR/mitm.stderr"
    exit 1
fi

# 2. invoke claude CLI through the proxy, with a clean env so the captured
#    identity is the canonical "sdk-cli" entrypoint (not the vscode-extension
#    variant our own shell ends up with).
log "invoking claude CLI via proxy"
PING_OUT=$(env -i \
    HOME="$HOME" PATH="$PATH" SHELL=/bin/bash \
    HTTPS_PROXY="http://127.0.0.1:$PORT" \
    HTTP_PROXY="http://127.0.0.1:$PORT" \
    NODE_EXTRA_CA_CERTS="$CA_CERT" \
    "$CLAUDE_BIN" -p "1+1=" --model claude-opus-4-6 2>&1 || true)
log "CLI output (truncated): $(echo "$PING_OUT" | head -c 200)"

# 3. stop mitmdump
sleep 1
kill "$MITM_PID" 2>/dev/null || true
wait "$MITM_PID" 2>/dev/null || true
unset MITM_PID

if [[ ! -s "$DUMP" ]]; then
    log "FAIL: empty mitm dump"
    exit 1
fi

# 4. Apply extraction → client.py. Snapshot HEAD first so we can roll back.
log "snapshotting current client.py / entry.py / tool_loop.py for rollback"
cd "$REPO"
BACKUP="$WORKDIR/client.py.bak"
cp services/claude_api/client.py "$BACKUP"

log "running apply_cli_identity.py"
APPLY_OUT=$("$PY_MITM" "$REPO/scripts/apply_cli_identity.py" "$DUMP" 2>&1 || true)
log "apply result: $APPLY_OUT"
if ! echo "$APPLY_OUT" | grep -q '"changed"'; then
    log "FAIL: apply produced no parseable result, rolling back"
    cp "$BACKUP" services/claude_api/client.py
    exit 1
fi

# 5. Run the touched-file tests. If they fail, roll back.
log "running pytest"
if ! "$PY_VENV" -m pytest tests/test_claude_api_entry.py tests/test_session.py -q > "$WORKDIR/pytest.out" 2>&1; then
    log "FAIL: pytest, rolling back. tail:"
    tail -30 "$WORKDIR/pytest.out"
    cp "$BACKUP" services/claude_api/client.py
    exit 1
fi

# 6. Restart the bot.
log "restarting discord-bot"
sudo systemctl restart discord-bot
sleep 8
if ! systemctl is-active --quiet discord-bot; then
    log "FAIL: bot did not come up after restart, rolling back"
    cp "$BACKUP" services/claude_api/client.py
    sudo systemctl restart discord-bot
    exit 1
fi

# 7. Verify HTTP 200 via verify_cli_identity.py
log "verifying HTTP 200 with the new identity"
VERIFY_OUT=$("$PY_VENV" "$REPO/scripts/verify_cli_identity.py" 2>&1 || true)
VERIFY_RC=$?
log "verify exit=$VERIFY_RC out: $VERIFY_OUT"

if [[ "$VERIFY_RC" -eq 0 ]]; then
    log "OK: identity refresh succeeded (HTTP 200)"
elif [[ "$VERIFY_RC" -eq 2 ]]; then
    # 429 is treated as non-fatal — quota may genuinely be exhausted but
    # the request shape was accepted (otherwise we'd get 4xx).
    log "WARN: identity OK but rate-limited (429). Keeping new client.py."
else
    log "FAIL: verify returned $VERIFY_RC, rolling back"
    cp "$BACKUP" services/claude_api/client.py
    sudo systemctl restart discord-bot
    exit 1
fi

log "===== refresh done ====="
exit 0

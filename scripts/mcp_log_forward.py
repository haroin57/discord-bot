#!/usr/bin/env python3
"""共有 MCP デーモン群 (mcp-*-shared / mcp-papermcp) の journal から
WARNING 以上と traceback を選定して Discord Webhook へバッチ転送する。
journal_forward.py / systemd_forward.py の姉妹スクリプト。stdlib のみ。

ログ形式が unit ごとに違う点に注意:
  A) "2026-07-04 07:54:09,014 INFO mem0-mcp: msg"   (logging カスタム format)
  B) "INFO:mcp.server.lowlevel.server:msg"           (logging デフォルト)
  C) "INFO:     127.0.0.1:36028 - POST /mcp 200 OK"  (uvicorn access)
  D) "[supergateway] StreamableHttp → Child: {json}" (oracle の中継ダンプ)
D の JSON-RPC ペイロードにはツールスキーマ由来の "error" 文字列が普通に
含まれるため、**中継行 (→ Child / Child →) は無条件に除外**してから
エラーマッチングする。traceback はレベル接頭辞を持たないので形状で拾う。

Webhook URL は環境変数 MCP_WEBHOOK_URL (EnvironmentFile 経由)。
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request

FLUSH_INTERVAL_S = 5
MAX_BATCH_CHARS = 1800
MAX_LINE_CHARS = 300  # oracle のペイロード等の巨大行対策

UNITS = [
    "mcp-mem0-shared",
    "mcp-second-opinion-shared",
    "mcp-oracle-shared",
    "mcp-x-scraper-shared",
    "mcp-papermcp",
]
TAG = {
    "mcp-mem0-shared.service": "mem0",
    "mcp-second-opinion-shared.service": "2nd-op",
    "mcp-oracle-shared.service": "oracle",
    "mcp-x-scraper-shared.service": "x-scr",
    "mcp-papermcp.service": "paper",
}

WEBHOOK_URL = os.environ.get("MCP_WEBHOOK_URL")
if not WEBHOOK_URL:
    sys.exit("MCP_WEBHOOK_URL is not set")

# 形式A: タイムスタンプ付き logging
FMT_A = re.compile(r"^\d{4}-\d{2}-\d{2} [\d:,]+ (\w+) ")
# 形式B: LEVEL:name: 接頭辞 (uvicorn の "INFO:     " も同型で拾われ INFO として弾かれる)
FMT_B = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):")
# supergateway の JSON-RPC 中継行 (誤検知源) — 無条件除外
RELAY = re.compile(r"^\[supergateway\].*(→ Child|Child → )")
# traceback / 例外 (レベル接頭辞なしで stderr に出る)
TRACEBACK = re.compile(r"^Traceback \(|^\s+File \"|^\w+(Error|Exception|Timeout)(:|$)|^\s+raise ")
# supergateway 自体の異常
SUPERGW_ERR = re.compile(r"^\[supergateway\].*(error|exited|crash|failed)", re.I)

LEVELS = {"WARNING", "ERROR", "CRITICAL"}


def select_line(unit: str, msg: str) -> str | None:
    if RELAY.search(msg):
        return None
    level = None
    m = FMT_A.match(msg)
    if m:
        level = m.group(1)
    else:
        m = FMT_B.match(msg)
        if m:
            level = m.group(1)
    picked = False
    if level in LEVELS:
        picked = True
    elif level is None and (TRACEBACK.search(msg) or SUPERGW_ERR.search(msg)):
        picked = True
    if not picked:
        return None
    tag = TAG.get(unit, unit)
    if len(msg) > MAX_LINE_CHARS:
        msg = msg[:MAX_LINE_CHARS] + " …(truncated)"
    return f"[{tag}] {msg}"


def send(content: str) -> None:
    body = json.dumps({"username": "かなえ MCP", "content": content}).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            # Discord/Cloudflare WAF は urllib デフォルト UA を 403 で弾く
            "User-Agent": "kanae-mcp-forward/1.0",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[mcp_log_forward] webhook send failed: {e}", file=sys.stderr)


def flush(buf: list[str]) -> None:
    if not buf:
        return
    text = "\n".join(buf)
    for i in range(0, len(text), MAX_BATCH_CHARS):
        send(f"```\n{text[i : i + MAX_BATCH_CHARS]}\n```")


def main() -> None:
    cmd = ["journalctl", "-f", "-o", "json", "-n", "0", "--since", "now"]
    for u in UNITS:
        cmd += ["-u", u]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
    )

    q: "queue.Queue[str]" = queue.Queue()

    def reader() -> None:
        for raw in proc.stdout:
            try:
                rec = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            msg = rec.get("MESSAGE")
            unit = rec.get("_SYSTEMD_UNIT", "")
            if not isinstance(msg, str):
                continue
            line = select_line(unit, msg)
            if line:
                q.put(line)

    threading.Thread(target=reader, daemon=True).start()

    buf: list[str] = []
    last_flush = time.monotonic()
    while True:
        timeout = max(0.0, FLUSH_INTERVAL_S - (time.monotonic() - last_flush))
        try:
            line = q.get(timeout=timeout)
            buf.append(line)
            if sum(len(l) + 1 for l in buf) >= MAX_BATCH_CHARS:
                flush(buf)
                buf = []
                last_flush = time.monotonic()
        except queue.Empty:
            if buf:
                flush(buf)
                buf = []
            last_flush = time.monotonic()


if __name__ == "__main__":
    main()

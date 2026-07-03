#!/usr/bin/env python3
"""C0d (2026-07-04 SRE): discord-bot の journal (WARNING 以上) を Discord Webhook
へバッチ転送する常駐デーモン。stdlib のみで動く — venv 非依存で systemd から直接起動する。

discord-bot は services.structured_log.JsonFormatter で1行1JSONのログを出す。
journalctl 自体の -p (syslog priority) はこの JSON 内の "level" とは無関係
（bot はどのレベルも同じ syslog priority で出力するため常に空振りする）ので、
journalctl 側はフィルタなしで流し、各行を JSON としてパースして
level in {WARNING, ERROR, CRITICAL} だけを選定する。JSON でない行（サードパーティ
ライブラリの生print等）は素通しせず捨てる。

Webhook URL は環境変数 JOURNAL_WEBHOOK_URL（EnvironmentFile 経由）。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

UNIT = "discord-bot"
SELECT_LEVELS = {"WARNING", "ERROR", "CRITICAL"}
FLUSH_INTERVAL_S = 5
MAX_BATCH_CHARS = 1800

WEBHOOK_URL = os.environ.get("JOURNAL_WEBHOOK_URL")
if not WEBHOOK_URL:
    sys.exit("JOURNAL_WEBHOOK_URL is not set")

import urllib.request


def send(content: str) -> None:
    # Discord/Cloudflare の WAF は urllib のデフォルト User-Agent を弾く(403) — 明示が必須。
    body = json.dumps({"username": "かなえ journal", "content": content}).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "kanae-journal-forward/1.0",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[journal_forward] webhook send failed: {e}", file=sys.stderr)


def flush(buf: list[str]) -> None:
    if not buf:
        return
    text = "\n".join(buf)
    for i in range(0, len(text), MAX_BATCH_CHARS):
        chunk = text[i : i + MAX_BATCH_CHARS]
        send(f"```\n{chunk}\n```")


def select_line(raw: str) -> str | None:
    """Return a short display line if raw is a JSON log record at WARNING+."""
    try:
        rec = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if rec.get("level") not in SELECT_LEVELS:
        return None
    ts = rec.get("ts", "")
    level = rec.get("level", "")
    logger = rec.get("logger", "")
    msg = rec.get("msg", "")
    line = f"[{ts}] {level} {logger}: {msg}"
    if rec.get("exc"):
        line += f"\n{rec['exc']}"
    return line


def main() -> None:
    proc = subprocess.Popen(
        ["journalctl", "-u", UNIT, "-f", "-o", "cat", "--since", "now"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )

    q: "queue.Queue[str]" = queue.Queue()

    def reader() -> None:
        for raw in proc.stdout:
            raw = raw.rstrip("\n")
            if not raw:
                continue
            line = select_line(raw)
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
            batch_len = sum(len(l) + 1 for l in buf)
            if batch_len >= MAX_BATCH_CHARS:
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

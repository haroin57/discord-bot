#!/usr/bin/env python3
"""systemd のユニットライフサイクルイベント (Started/Stopped/Failed 等) を
Discord Webhook へバッチ転送する常駐デーモン。stdlib のみ、venv 非依存。

journal_forward.py (C0d) の姉妹スクリプト。あちらは discord-bot の
アプリログ (JSON) を level で選定するが、こちらは PID 1 (systemd) の
メッセージからユニット状態遷移だけを拾う。定期ジョブ由来のノイズ
(sysstat / apt-daily / session scope 等) は BLOCK パターンで落とす。

Webhook URL は環境変数 SYSTEMD_WEBHOOK_URL (EnvironmentFile 経由)。
システム journal の読み取りには systemd-journal グループが必要
(unit 側で SupplementaryGroups=systemd-journal を付与)。
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

WEBHOOK_URL = os.environ.get("SYSTEMD_WEBHOOK_URL")
if not WEBHOOK_URL:
    sys.exit("SYSTEMD_WEBHOOK_URL is not set")

# 拾う: ユニットの状態遷移と失敗詳細
SELECT = re.compile(
    r"^(Started|Stopped|Starting|Stopping|Failed to start|Reloaded) "
    r"|^\S+\.service: (Main process exited|Failed with result|Scheduled restart)"
)
# 落とす: 定期ジョブ・セッション scope・自明なノイズ
BLOCK = re.compile(
    r"sysstat-collect|apt-daily|logrotate|man-db|motd-news|phpsessionclean"
    r"|snapd\.|fwupd|e2scrub|dpkg-db-backup|systemd-tmpfiles"
    r"|session-\d+\.scope|user@\d+\.service|user-runtime-dir"
    r"|run-r[0-9a-f]+|sleep\.target|suspend\.target"
)

EMOJI = [
    (re.compile(r"^Failed to start|Failed with result|Main process exited"), "🔴"),
    (re.compile(r"^Scheduled restart|^\S+\.service: Scheduled restart"), "🟠"),
    (re.compile(r"^Started "), "🟢"),
    (re.compile(r"^(Stopped|Stopping) "), "⚪"),
    (re.compile(r"^(Starting|Reloaded) "), "🔵"),
]


def decorate(msg: str) -> str:
    for pat, emoji in EMOJI:
        if pat.search(msg):
            return f"{emoji} {msg}"
    return f"▫️ {msg}"


def send(content: str) -> None:
    body = json.dumps({"username": "かなえ systemd", "content": content}).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            # Discord/Cloudflare WAF は urllib デフォルト UA を 403 で弾く
            "User-Agent": "kanae-systemd-forward/1.0",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[systemd_forward] webhook send failed: {e}", file=sys.stderr)


def flush(buf: list[str]) -> None:
    if not buf:
        return
    text = "\n".join(buf)
    for i in range(0, len(text), MAX_BATCH_CHARS):
        send(text[i : i + MAX_BATCH_CHARS])


def select_line(raw: str) -> str | None:
    try:
        rec = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    msg = rec.get("MESSAGE")
    if not isinstance(msg, str):
        return None
    if not SELECT.search(msg) or BLOCK.search(msg):
        return None
    ts = time.strftime("%H:%M:%S", time.localtime(int(rec.get("__REALTIME_TIMESTAMP", "0")) / 1e6))
    return f"`{ts}` {decorate(msg)}"


def main() -> None:
    proc = subprocess.Popen(
        ["journalctl", "-f", "-o", "json", "_PID=1", "-n", "0", "--since", "now"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )

    q: "queue.Queue[str]" = queue.Queue()

    def reader() -> None:
        for raw in proc.stdout:
            line = select_line(raw.rstrip("\n"))
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

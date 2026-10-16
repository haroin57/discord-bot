#!/usr/bin/env python3
"""常駐デーモン: Claude Code CLI の UserPromptSubmit hook 用 Unix ソケット窓口。

検索そのものは mcp-mem0-shared（POST /hook/search）が行い、ここは stdlib だけの
proxy に徹する。以前はこのプロセスが Mem0 スタック全体と v1 連想 index を
もう 1 セット抱えて約 300MB 常駐していた（2026-10-16 memory-diet）。
mem0_hook_search.py 側のワイヤープロトコル（1 行クエリ → ブロック返却 → close）と、
失敗時に空ブロックを返す挙動は従来どおり。
"""

import asyncio
import os
import sys
import time
import urllib.request

SOCK_PATH = os.environ.get("MEM0_HOOK_SOCK", "/tmp/mem0_hook.sock")
HOOK_URL = os.environ.get("MEM0_HOOK_URL", "http://127.0.0.1:8013/hook/search")
# クライアント側は 5 秒で諦めるので、それより前に（空でも）返す。
HTTP_TIMEOUT = float(os.environ.get("MEM0_HOOK_HTTP_TIMEOUT", "4.5"))
MAX_QUERY_BYTES = 16_384
# 失敗は空ブロックに劣化させるが、404（route 未デプロイ）や接続拒否（MCP 停止）が
# journal で見えるように、種類だけ 1 分に 1 回まで出す。
_ERROR_LOG_INTERVAL = 60.0
_last_error_log = 0.0


def _log_failure(exc: BaseException) -> None:
    global _last_error_log
    now = time.monotonic()
    if now - _last_error_log >= _ERROR_LOG_INTERVAL:
        _last_error_log = now
        print(
            f"mem0_hook_daemon: backend failed ({type(exc).__name__}: {exc})",
            file=sys.stderr,
            flush=True,
        )


def _fetch_block(query: str) -> str:
    req = urllib.request.Request(
        HOOK_URL,
        data=query.encode("utf-8")[:MAX_QUERY_BYTES],
        headers={"Content-Type": "text/plain; charset=utf-8", "X-Kanae-Hook": "1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


async def handle(reader, writer):
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=10)
        query = raw.decode("utf-8", "replace").strip()
        block = ""
        if query:
            try:
                block = await asyncio.to_thread(_fetch_block, query)
            except Exception as exc:
                _log_failure(exc)
                block = ""
        writer.write(block.encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()


async def main():
    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)
    server = await asyncio.start_unix_server(handle, path=SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    print(f"mem0_hook_daemon listening on {SOCK_PATH} -> {HOOK_URL}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

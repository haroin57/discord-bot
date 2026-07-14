#!/usr/bin/env python3
"""常駐デーモン: Mem0を1回だけ初期化し、Unixソケットでクエリを待ち受ける。
Claude Code CLI の UserPromptSubmit hook からの高速な記憶検索用。
"""
import asyncio
import os
import sys

sys.path.insert(0, "/home/ubuntu/discord-bot")

from services.mem0.search import search_memories_smart
from services.mem0.format import format_memories_for_prompt

USER_ID = "573489206240870410"
SOCK_PATH = "/tmp/mem0_hook.sock"


async def handle(reader, writer):
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=10)
        query = raw.decode("utf-8", "replace").strip()
        block = ""
        if query:
            try:
                results = await search_memories_smart(
                    query, USER_ID, limit=4,
                    rewrite=False, rerank=False, include_core=True,
                )
                block = format_memories_for_prompt(results) or ""
            except Exception:
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
    print(f"mem0_hook_daemon listening on {SOCK_PATH}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

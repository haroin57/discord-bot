"""Measure Discord's REAL single-message edit rate-limit bucket via raw REST.

POST a throwaway message, PATCH (edit) it back-to-back ~24 times while logging
the rate-limit response headers (X-RateLimit-Limit / Remaining / Reset-After)
and any 429 retry, then DELETE it. The headers reveal the exact bucket shape so
we can pace the spinner just under it (avoid 429 entirely) instead of guessing.
"""
import asyncio
import time
import os
import sys

import aiohttp

CHANNEL_ID = "1488369795807055964"
N_EDITS = 24


def load_token() -> str:
    # systemd env first, then .env
    tok = os.environ.get("DISCORD_TOKEN", "").strip()
    if tok:
        return tok
    with open("/home/ubuntu/discord-bot/.env") as f:
        for line in f:
            if line.startswith("DISCORD_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no DISCORD_TOKEN")


async def main():
    token = load_token()
    base = "https://discord.com/api/v10"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as s:
        # POST a throwaway message
        async with s.post(f"{base}/channels/{CHANNEL_ID}/messages",
                          json={"content": "⏳ レート測定中… (自動削除)"}) as r:
            if r.status != 200:
                print("POST failed", r.status, await r.text())
                return
            msg = await r.json()
            mid = msg["id"]

        url = f"{base}/channels/{CHANNEL_ID}/messages/{mid}"
        print(f"{'#':>2} {'dt(s)':>6} {'status':>6} {'limit':>5} {'remain':>6} {'reset_after':>11} {'retry':>6}")
        prev = time.monotonic()
        try:
            i = 0
            while i < N_EDITS:
                async with s.patch(url, json={"content": f"⏳ 測定 frame {i} ({i}s)"}) as r:
                    now = time.monotonic()
                    dt = now - prev
                    prev = now
                    lim = r.headers.get("X-RateLimit-Limit", "-")
                    rem = r.headers.get("X-RateLimit-Remaining", "-")
                    reset_after = r.headers.get("X-RateLimit-Reset-After", "-")
                    retry = "-"
                    if r.status == 429:
                        body = await r.json()
                        retry = str(body.get("retry_after", r.headers.get("Retry-After", "-")))
                    print(f"{i:>2} {dt:6.3f} {r.status:>6} {lim:>5} {rem:>6} {reset_after:>11} {retry:>6}")
                    if r.status == 429:
                        await asyncio.sleep(float(retry))
                        # don't advance i: retry the same frame after backoff
                        continue
                i += 1
        finally:
            async with s.delete(url) as r:
                print(f"\ncleanup DELETE status={r.status}")


asyncio.run(main())

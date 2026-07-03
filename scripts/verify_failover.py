"""OAuth 二重アカウントの検証 — primary / failover 両方の疎通と identity 分離を確認。

各 credentials ファイルの token で GET /api/oauth/profile を叩き、
アカウントが別物であること・token が生きていることを確認する。
token そのものは絶対に表示しない（email と uuid 先頭8字のみ）。

実行: .venv/bin/python scripts/verify_failover.py
"""

import asyncio
import json
import sys

import aiohttp

sys.path.insert(0, "/home/ubuntu/discord-bot")

from services.oauth_manager import (  # noqa: E402
    CREDENTIALS_PATH,
    FAILOVER_CREDENTIALS_PATH,
)

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


def _load_token(path: str) -> "str | None":
    try:
        with open(path) as f:
            data = json.load(f)
        return (data.get("claudeAiOauth") or {}).get("accessToken")
    except (OSError, json.JSONDecodeError):
        return None


async def _probe(name: str, path: str) -> "str | None":
    token = _load_token(path)
    if not token:
        print(f"{name}: ❌ credentials 読めず ({path})")
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "axios/1.13.6",
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(PROFILE_URL, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    print(f"{name}: ❌ profile HTTP {resp.status}（token 失効の可能性）")
                    return None
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: ❌ {type(exc).__name__}: {exc}")
        return None
    acct = data.get("account") or {}
    email = acct.get("email") or acct.get("email_address") or "?"
    uuid = str(acct.get("uuid") or acct.get("account_uuid") or "?")[:8]
    org = (data.get("organization") or {}).get("name", "?")
    print(f"{name}: ✅ email={email} uuid={uuid}… org={org}")
    return uuid


async def main() -> int:
    print(f"primary : {CREDENTIALS_PATH}")
    print(f"failover: {FAILOVER_CREDENTIALS_PATH}")
    p = await _probe("primary ", CREDENTIALS_PATH)
    f = await _probe("failover", FAILOVER_CREDENTIALS_PATH)
    if p and f:
        if p == f:
            print("⚠️ 両ファイルが同一アカウントを指しています — 移行手順を確認")
            return 1
        print("OK: 2アカウントとも疎通し、別アカウントであることを確認")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

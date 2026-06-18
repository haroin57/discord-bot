"""ザラ場 VI / 日経 取得モジュール (Phase 1A の基盤, 2026-06-15)

Yahoo Finance Japan + フィスコ記事から、ザラ場時間中の
VI 水準と日経平均ザラ場値を取得する。

設計母体: /home/ubuntu/mcp-servers/yfj-quote/mcp_server.py
- HTTPオーケストレーション (throttle/retry/JWT) はここに移植 (subprocess 化のため)
- パース (記事抽出 / HTML→JSON) は yfj_parser.py / fisco_news.py を sys.path 経由で直接 import

依存:
- httpx 0.28.1 (discord-bot/.venv 既導入)
- /home/ubuntu/mcp-servers/yfj-quote/{fisco_news,yfj_parser}.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from urllib.parse import quote as urlquote

import httpx

# パースロジックは yfj-quote MCP サーバーから直接 import (DRY)
sys.path.insert(0, "/home/ubuntu/mcp-servers/yfj-quote")
import fisco_news  # noqa: E402
import yfj_parser  # noqa: E402
from yfj_parser import YfjParseError  # noqa: E402

log = logging.getLogger(__name__)

BASE = "https://finance.yahoo.co.jp"
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}
_TIMEOUT = 30
_MIN_INTERVAL_S = 1.0

_client: httpx.AsyncClient | None = None
_throttle_lock: asyncio.Lock | None = None
_last_request_at: float = 0.0
_jwt_cache: dict[str, tuple[float, str]] = {}
_JWT_TTL_S = 10 * 60.0


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers=UA, timeout=_TIMEOUT, follow_redirects=True,
        )
    return _client


def _get_throttle_lock() -> asyncio.Lock:
    global _throttle_lock
    if _throttle_lock is None:
        _throttle_lock = asyncio.Lock()
    return _throttle_lock


async def _get(url: str, *, headers: dict | None = None) -> httpx.Response:
    """1秒1リクエスト throttle + 1リトライ付き HTTP GET。"""
    global _last_request_at
    client = _get_client()
    lock = _get_throttle_lock()
    async with lock:
        last_exc: Exception | None = None
        for attempt in (1, 2):
            wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request_at = time.monotonic()
            try:
                resp = await client.get(url, headers=headers)
            except httpx.TransportError as exc:
                last_exc = exc
                continue
            if resp.status_code >= 500 and attempt == 1:
                continue
            return resp
        raise last_exc or RuntimeError("request failed after retry")


async def _news_jwt(force_refresh: bool = False) -> str:
    """ニュース検索 BFF API 用 JWT 取得 (10分キャッシュ)。"""
    if not force_refresh:
        cached = _jwt_cache.get("token")
        if cached and time.monotonic() - cached[0] < _JWT_TTL_S:
            return cached[1]
    url = f"{BASE}/news/search?query={urlquote('日経VI')}"
    resp = await _get(url)
    resp.raise_for_status()
    state = yfj_parser.extract_state(resp.text)
    jwt = yfj_parser.parse_news_jwt(state)
    _jwt_cache["token"] = (time.monotonic(), jwt)
    return jwt


async def fetch_intraday_vi() -> dict:
    """フィスコ記事から最新の VI 水準を取得。

    Returns:
        成功: {found: True, vi_current, vi_change, direction, vi_high, vi_low,
               headline, article_time, article_url}
        失敗: {found: False, stage, reason, ...}
    """
    api_url = (
        f"{BASE}/bff-pc-news/v1/main/news/search"
        f"?query={urlquote('日経VI')}&searchSupport=andQuery&page=1&size=10"
    )
    try:
        jwt = await _news_jwt()
        resp = await _get(api_url, headers={"jwt-token": jwt})
        if resp.status_code == 401:
            jwt = await _news_jwt(force_refresh=True)
            resp = await _get(api_url, headers={"jwt-token": jwt})
        resp.raise_for_status()
        articles = yfj_parser.parse_news_search(resp.json())
    except YfjParseError as exc:
        return {
            "found": False,
            "stage": "search",
            "reason": f"schema drift at {exc.path}: {exc.detail}",
        }
    except Exception as exc:
        return {"found": False, "stage": "search", "reason": str(exc)}

    art = fisco_news.pick_latest_vi_article(articles)
    if art is None:
        return {
            "found": False,
            "stage": "search",
            "reason": "no 日経VI article in latest results",
        }

    try:
        resp = await _get(art["url"])
        resp.raise_for_status()
        detail = yfj_parser.parse_article(yfj_parser.extract_state(resp.text))
    except YfjParseError as exc:
        return {
            "found": False,
            "stage": "article",
            "reason": f"schema drift at {exc.path}: {exc.detail}",
            "article_url": art["url"],
        }
    except Exception as exc:
        return {
            "found": False,
            "stage": "article",
            "reason": str(exc),
            "article_url": art["url"],
        }

    numbers = fisco_news.extract_vi_numbers(detail["body"])
    if numbers["vi_current"] is None:
        return {
            "found": False,
            "stage": "extract",
            "reason": "VI level pattern not found",
            "article_url": art["url"],
            "headline": detail.get("headline"),
        }

    return {
        "found": True,
        "headline": detail["headline"],
        "article_time": detail.get("time"),
        "article_url": art["url"],
        **numbers,
    }


async def fetch_intraday_nikkei(code: str = "998407.O") -> dict:
    """日経平均 (or 任意銘柄) のザラ場値を Yahoo Finance Japan から取得。

    Args:
        code: 既定 "998407.O" (日経平均)。"998405.T" (TOPIX) など可。

    Returns:
        {name, code, market, price, change, change_rate_pct, previous_close,
         open, high, low, volume, market_cap_million_yen, update_time_jp}
    """
    url = f"{BASE}/quote/{urlquote(code)}"
    resp = await _get(url)
    resp.raise_for_status()
    return yfj_parser.parse_quote(resp.text)


async def close_client() -> None:
    """httpx client クリーンアップ (テスト終了時)。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# --- CLI テスト ---
async def _main():
    print("=== Intraday VI (Fisco news) ===")
    vi = await fetch_intraday_vi()
    print(json.dumps(vi, ensure_ascii=False, indent=2))
    print()
    print("=== Intraday Nikkei (Yahoo Finance JP) ===")
    nk = await fetch_intraday_nikkei()
    print(json.dumps(nk, ensure_ascii=False, indent=2))
    await close_client()


if __name__ == "__main__":
    asyncio.run(_main())

#!/usr/bin/env python3
"""為替介入アラート・スコアモデル (ADR-0027)

30分ごとに6シグナルをスコアリングし、合計8点超でDiscordアラートを飛ばす。
手動実行: python scripts/intervention_alert.py [--dry-run]

シグナル:
  ①水準 ②変動ペース ⑤時間帯 ⑥Polymarket(165到達) = 実装済み
  ③口先介入 ④レートチェック報道 = Phase2スタブ(0点固定、ニュース解析を後で追加)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

import jpholiday
import requests
import yfinance as yf

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("VI_ALERT_DISCORD_CHANNEL_ID", "1488369795807055964")
MENTION_USERS = ["573489206240870410", "1037285234200297522"]  # haroin, b0nchichi
THRESHOLD = 8
POLY_SLUG = "will-usdjpy-hit-165-high-in-2026"


def fetch_usdjpy() -> tuple[float, float, float]:
    """ドル円の現値・4時間足BB(20本)のMA・標準偏差を返す。"""
    df = yf.download("USDJPY=X", period="60d", interval="4h", progress=False)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    cur = float(df["Close"].iloc[-1])
    ma = float(df["Close"].rolling(20).mean().iloc[-1])
    sd = float(df["Close"].rolling(20).std().iloc[-1])
    return cur, ma, sd


def s_level(rate: float) -> tuple[int, str]:
    if rate >= 163:
        return 5, f"163超 {rate:.2f}"
    if rate >= 162:
        return 3, f"162台 {rate:.2f}"
    if rate >= 161:
        return 1, f"161台 {rate:.2f}"
    return 0, f"160台以下 {rate:.2f}"


def s_pace_bb(rate: float, ma: float, sd: float) -> tuple[int, str]:
    """4時間足BBの±σ乖離(zスコア)で行き過ぎを測る。
    円ベース(絶対値)でなくボラ正規化(相対値)=Wemof思想。"""
    if sd <= 0:
        return 0, "σ=0(算出不可)"
    z = (rate - ma) / sd
    az = abs(z)
    if az >= 2:
        return 3, f"{z:+.1f}σ ±2σ超 行き過ぎ"
    if az >= 1.5:
        return 1, f"{z:+.1f}σ"
    return 0, f"{z:+.1f}σ バンド内"


def s_time() -> tuple[int, str]:
    now = datetime.now()
    thin = (0 <= now.hour < 7) or jpholiday.is_holiday(now.date()) or now.weekday() >= 5
    return (2, "薄商い時間帯") if thin else (0, "通常時間帯")


def s_poly() -> tuple[int, str]:
    """Polymarket: USD/JPY 165到達確率。"""
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/markets?slug={POLY_SLUG}", timeout=15
        )
        d = r.json()
        prices = d[0].get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        pct = float(prices[0]) * 100
        if pct > 55:
            return 3, f"165到達{pct:.0f}% 円安加速"
        if pct >= 45:
            return 1, f"165到達{pct:.0f}%"
        return 0, f"165到達{pct:.0f}%"
    except Exception as e:
        return 0, f"Poly取得不可({type(e).__name__})"


def s_verbal() -> tuple[int, str]:
    """③口先介入 — Phase2 (ニュース/LLM解析を後で実装)。"""
    return 0, "口先介入 未実装(Phase2)"


def s_ratecheck() -> tuple[int, str]:
    """④レートチェック報道 — Phase2。"""
    return 0, "レートチェック 未実装(Phase2)"


def send_discord(content: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[DRY-RUN DISCORD]\n{content}")
        return True
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set in .env", file=sys.stderr)
        return False
    resp = requests.post(
        f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
        headers={
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"content": content, "allowed_mentions": {"users": MENTION_USERS}},
        timeout=15,
    )
    ok = resp.status_code in (200, 201)
    print(f"{'OK' if ok else 'NG'} Discord status={resp.status_code}")
    if not ok:
        print(resp.text, file=sys.stderr)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="為替介入アラート (ADR-0027)")
    ap.add_argument("--dry-run", action="store_true", help="送信せず表示のみ")
    args = ap.parse_args()

    rate, ma, sd = fetch_usdjpy()
    rows = [
        ("①水準", *s_level(rate)),
        ("②変動ペース(±σ)", *s_pace_bb(rate, ma, sd)),
        ("⑤時間帯", *s_time()),
        ("⑥Polymarket", *s_poly()),
        ("③口先介入", *s_verbal()),
        ("④レートチェック", *s_ratecheck()),
    ]
    total = sum(r[1] for r in rows)
    print(f"=== 介入スコア {datetime.now():%m-%d %H:%M} 合計 {total}/{THRESHOLD} ===")
    for lab, pts, desc in rows:
        print(f"  {lab}: {pts}点 ({desc})")

    if total > THRESHOLD:
        active = "\n".join(
            f"・{lab} {pts}点（{desc}）" for lab, pts, desc in rows if pts > 0
        )
        mentions = " ".join(f"<@{u}>" for u in MENTION_USERS)
        msg = (
            f"{mentions}\n"
            f"⚠️ **為替介入アラート: {total}点**（閾値{THRESHOLD}）\n"
            f"ドル円 **{rate:.2f}**\n\n{active}\n\n"
            f"-- \n介入アラート (ADR-0027 / 送信元: かなえ bot)"
        )
        send_discord(msg, dry_run=args.dry_run)
        print(f"\n>>> 閾値超過、{'(dry-run)' if args.dry_run else '通知送信'}")
    else:
        print(f"\n閾値未満、通知なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())

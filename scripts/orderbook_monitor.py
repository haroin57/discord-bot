#!/usr/bin/env python3
"""
OANDAオーダーブック監視 (ADR-0028)

30分ごとに OANDA v20 の orderBook/positionBook を取得し、
ショート/ロング建玉・注文が大きく溜まっている価格帯を Discord 通知する。

通知2系統 (両方):
  A. 定期通知 : 毎回、現在の濃い価格帯トップを淡々と出す
  B. 変化検知 : 前回スナップショットと比較し、集中度が大きく動いた帯を強調
              (踏み上げ/介入の予兆)。変化時のみメンション付与。

データ: OANDA v20 /v3/instruments/USD_JPY/{orderBook,positionBook}
        bucketWidth=0.05, buckets≈1440, 各 price/longCountPercent/shortCountPercent
認証  : .env の OANDA_API_TOKEN / OANDA_ENV (practice/live で host 分岐)
通知  : Bot API + VI_ALERT_DISCORD_CHANNEL_ID (外部 webhook へは一切送らない)
状態  : ~/.local/state/discord-bot/orderbook_state.json
使い方: python3 scripts/orderbook_monitor.py [--dry-run]
"""
import os
import sys
import json
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.path.expanduser("~/.local/state/discord-bot"))
STATE_FILE = STATE_DIR / "orderbook_state.json"

INSTRUMENT = "USD_JPY"
WINDOW = 1.5         # 現在値から±何円を監視対象にするか
CLUSTER_GAP = 0.10   # この値以内の隣接バケットを同一ゾーンに併合
TOP_N = 3            # 各カテゴリの報告ゾーン数
CHANGE_ABS = 0.20    # 集中度(%)がこれ以上動いたら「変化」とみなす(絶対pt)


# ---------------------------------------------------------------- env
def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    # 実環境変数を優先(systemd EnvironmentFile 等)
    for k in ("OANDA_API_TOKEN", "OANDA_ENV", "DISCORD_TOKEN",
              "VI_ALERT_DISCORD_CHANNEL_ID", "HAROIN_DISCORD_ID",
              "BONCHICHI_DISCORD_ID"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


ENV = load_env()


def api_host() -> str:
    return ("https://api-fxpractice.oanda.com"
            if "pract" in ENV.get("OANDA_ENV", "practice").lower()
            else "https://api-fxtrade.oanda.com")


# ---------------------------------------------------------------- fetch
def fetch_book(ep: str) -> dict:
    token = ENV.get("OANDA_API_TOKEN", "")
    if not token:
        raise RuntimeError("OANDA_API_TOKEN 未設定")
    r = requests.get(
        f"{api_host()}/v3/instruments/{INSTRUMENT}/{ep}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()[ep]


def window_buckets(book: dict, price: float) -> dict:
    """現在値±WINDOW のバケットを {price: (long%, short%)} で返す。"""
    lo, hi = price - WINDOW, price + WINDOW
    out = {}
    for b in book["buckets"]:
        p = float(b["price"])
        if lo <= p <= hi:
            out[f"{p:.3f}"] = (
                float(b["longCountPercent"]),
                float(b["shortCountPercent"]),
            )
    return out


# ---------------------------------------------------------------- zones
def merge_zones(items):
    """items: [(price, value)] → 隣接(≤CLUSTER_GAP)併合 [(lo,hi,sum,peak,peakv)] 降順。"""
    picked = sorted((p, v) for p, v in items if v > 0)
    zones = []
    for p, v in picked:
        if zones and p - zones[-1][1] <= CLUSTER_GAP + 1e-9:
            lo, hi, tot, pk, pkv = zones[-1]
            hi, tot = p, tot + v
            if v > pkv:
                pk, pkv = p, v
            zones[-1] = (lo, hi, tot, pk, pkv)
        else:
            zones.append((p, p, v, p, v))
    zones.sort(key=lambda z: -z[2])
    return zones


def top_zones(win: dict, side: str, price: float, direction=None):
    """side='short'|'long'. direction='above'|'below'|None で現在値基準にフィルタ。"""
    idx = 1 if side == "short" else 0
    items = []
    for ps, vals in win.items():
        p = float(ps)
        if direction == "above" and not p > price:
            continue
        if direction == "below" and not p < price:
            continue
        items.append((p, vals[idx]))
    return merge_zones(items)[:TOP_N]


def fmt_zone(z) -> str:
    lo, hi, tot, pk, pkv = z
    rng = f"{lo:.2f}" if abs(hi - lo) < 1e-9 else f"{lo:.2f}〜{hi:.2f}"
    return f"{rng} (計{tot:.2f}%/山{pk:.2f})"


def fmt_line(zones) -> str:
    return " ・ ".join(fmt_zone(z) for z in zones) if zones else "—"


# ---------------------------------------------------------------- change
def detect_changes(prev: dict, cur_pos: dict, cur_ord: dict, price: float):
    """前回 state と比較し、集中度が CHANGE_ABS 以上動いた価格帯を返す。"""
    msgs = []
    for book_key, cur, label in (
        ("pos", cur_pos, "建玉"),
        ("ord", cur_ord, "注文"),
    ):
        before = (prev or {}).get(book_key, {})
        movers = []
        for ps, (lng, sht) in cur.items():
            blng, bsht = before.get(ps, (0.0, 0.0))
            dl, ds = lng - blng, sht - bsht
            for d, side in ((ds, "S"), (dl, "L")):
                if abs(d) >= CHANGE_ABS:
                    movers.append((abs(d), float(ps), side, d))
        movers.sort(reverse=True)
        for _, p, side, d in movers[:3]:
            arrow = "増" if d > 0 else "減"
            rel = "上" if p > price else ("下" if p < price else "現値")
            sidej = "ショート" if side == "S" else "ロング"
            msgs.append(f"{label} {p:.2f}({rel}) {sidej} {d:+.2f}pt {arrow}")
    return msgs


# ---------------------------------------------------------------- discord
def send_discord(msg: str, mention: bool):
    token = ENV.get("DISCORD_TOKEN", "")
    ch = ENV.get("VI_ALERT_DISCORD_CHANNEL_ID", "")
    if not token or not ch:
        print("[discord] token/channel 未設定、送信スキップ", file=sys.stderr)
        return
    content = msg
    if mention:
        ms = []
        if ENV.get("HAROIN_DISCORD_ID"):
            ms.append(f"<@{ENV['HAROIN_DISCORD_ID']}>")
        if ENV.get("BONCHICHI_DISCORD_ID"):
            ms.append(f"<@{ENV['BONCHICHI_DISCORD_ID']}>")
        if ms:
            content = " ".join(ms) + "\n" + msg
    r = requests.post(
        f"https://discord.com/api/v10/channels/{ch}/messages",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        json={"content": content, "allowed_mentions": {"parse": ["users"]}},
        timeout=10,
    )
    if r.status_code >= 300:
        print(f"[discord] 送信失敗 {r.status_code}: {r.text}", file=sys.stderr)
    else:
        print("[discord] 送信成功", file=sys.stderr)


# ---------------------------------------------------------------- main
def build_report():
    pb = fetch_book("positionBook")
    ob = fetch_book("orderBook")
    price = float(pb["price"])
    snap = pb.get("time", "")[11:16]
    pos = window_buckets(pb, price)
    ordr = window_buckets(ob, price)

    pos_short = top_zones(pos, "short", price)
    pos_long = top_zones(pos, "long", price)
    # 注文: 現値より上の買い(=ショート損切りbuy stop), 下の売り(=ロング損切りsell stop)
    ord_buystop = top_zones(ordr, "long", price, direction="above")
    ord_sellstop = top_zones(ordr, "short", price, direction="below")

    lines = [
        f"📊 **ドル円オーダーブック** ({snap} snapshot / {price:.2f})",
        f"▼建玉 ショート濃: {fmt_line(pos_short)}",
        f"▼建玉 ロング濃 : {fmt_line(pos_long)}",
        f"▼注文 ショート損切り(上の買): {fmt_line(ord_buystop)}",
        f"▼注文 ロング損切り(下の売) : {fmt_line(ord_sellstop)}",
    ]
    state = {"price": price, "time": pb.get("time", ""),
             "pos": pos, "ord": ordr}
    return "\n".join(lines), state, price


def main():
    dry = "--dry-run" in sys.argv
    prev = None
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text())
        except Exception:
            prev = None

    report, state, price = build_report()
    changes = detect_changes(prev, state["pos"], state["ord"], price)

    msg = report
    if changes:
        msg += "\n⚡**変化**: " + " / ".join(changes)

    print(msg)
    if changes:
        print("[info] 変化検知あり → メンション付与", file=sys.stderr)

    if not dry:
        send_discord(msg, mention=bool(changes))
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))
    else:
        print("[dry-run] 送信・状態保存スキップ", file=sys.stderr)


if __name__ == "__main__":
    main()

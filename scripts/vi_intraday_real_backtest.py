#!/usr/bin/env python3
"""
VI日内変動 平均回帰戦略 — 日経VI実値OHLC版（29回の決定版再現）

データ出所（重要・無料）:
  日経公式 indexes.nikkei.co.jp のヒストリカルCSV。OHLC全列入り・cp932。
    - 日経VI : nikkei_stock_average_vi_daily_jp.csv
    - 日経平均: nikkei_stock_average_daily_jp.csv
  ※ ファイル名は "volatility_index" ではなく "stock_average_vi"。
    J-Quants(有料) も楽天証券の内部APIも不要。プロキシ(HV22+VRP)も不要。

発火条件（3つ。b0nchichi の「日内変動がキモ」の正体）:
  (1) VI始値 ≥ 30        … 恐怖の高原にいる
  (2) VI終値 > VI始値    … 場中に恐怖が製造される（red day）
  (3) 日経終値 < 日経始値 … 価格も場中に垂れる（px down）
  → その日の引けで日経をロング、5営業日後に手仕舞い。

実測（2023-01-04〜2026-06-12, 841営業日）:
  red 378(44.9%) / signal 242(28.8%) / signal&VI30 = 29回
  29回成績: +5d 中央値+3.08% 勝率79% 最悪-4.21%

注意:
  発火が時期に偏在（2023:0 2024:4 2025:9 2026:16/半年）。基準点30は
  日経6万・VI30常態化のレジームで陳腐化。「年1回」相当に絞るなら閾値VI40〜50。

実行: python3 scripts/vi_intraday_real_backtest.py
"""
from __future__ import annotations

import io
import urllib.request
import warnings

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126"}
VI_URL = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv"
NK_URL = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv"


def fetch(url: str) -> pd.DataFrame:
    req = urllib.request.Request(url, headers=UA)
    raw = urllib.request.urlopen(req, timeout=30).read().decode("cp932")
    df = pd.read_csv(io.StringIO(raw))
    df.columns = ["date", "close", "open", "high", "low"]
    df = df[df.date.str.match(r"\d{4}/")]
    df["date"] = pd.to_datetime(df.date)
    for c in ["close", "open", "high", "low"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.set_index("date")


def build(base: float = 30.0) -> pd.DataFrame:
    vi = fetch(VI_URL)
    nk = fetch(NK_URL)
    d = pd.DataFrame(
        {"vio": vi.open, "vic": vi.close, "nko": nk.open, "nkc": nk.close}
    ).dropna()
    d["red"] = d.vic > d.vio
    d["px_dn"] = d.nkc < d.nko
    d["sig"] = d.red & d.px_dn
    d["fire"] = d.sig & (d.vio >= base)
    for h in (1, 3, 5):
        d[f"fwd{h}"] = d.nkc.shift(-h) / d.nkc - 1
    return d


def report(d: pd.DataFrame, base: float = 30.0) -> None:
    n = len(d)
    print(f"sample: {d.index[0].date()} - {d.index[-1].date()}  days={n}")
    print(f"red days (VI close>open): {d.red.sum()}  ({d.red.mean()*100:.1f}%)")
    print(f"signal (red & px down):   {d.sig.sum()}  ({d.sig.mean()*100:.1f}%)")
    for label, mask in [
        ("baseline(all)", pd.Series(True, index=d.index)),
        ("signal", d.sig),
        (f"signal & VI_open>={base:.0f}", d.fire),
    ]:
        sub = d[mask]
        print(f"\n[{label}] n={len(sub)}")
        for h in (1, 3, 5):
            col = sub[f"fwd{h}"].dropna() * 100
            if len(col):
                print(
                    f"  +{h}d: median{col.median():+.2f}%  "
                    f"win{(col>0).mean()*100:.0f}%  worst{col.min():+.2f}%"
                )
    fire = d[d.fire]
    print(f"\n=== 発火日一覧 (VI始値>={base:.0f} & 場中VI上昇 & 日経場中下落): {len(fire)}回 ===")
    for dt, r in fire.iterrows():
        f5 = f"{r.fwd5*100:+.2f}%" if not np.isnan(r.fwd5) else "(live)"
        print(f"  {dt.date()}  VI {r.vio:.1f}->{r.vic:.1f}  NK {r.nko:.0f}->{r.nkc:.0f}  fwd5={f5}")
    # 年別頻度（レジーム偏在の確認）
    print("\n=== 年別発火回数 ===")
    print(fire.groupby(fire.index.year).size().to_string())


if __name__ == "__main__":
    BASE = 30.0
    d = build(BASE)
    report(d, BASE)

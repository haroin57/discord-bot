#!/usr/bin/env python3
"""
VI戦略 当日シグナルチェック（ADR-0011 rev3 確定版）

発火条件（①〜③）+ 条件⑥クレジット平穏フィルタを最新データで判定して表示する。
  ① VI始値 ≥ 30
  ② VI終値 > VI始値（場中に恐怖製造）
  ③ 日経終値 < 日経始値（価格も場中に垂れる）
  ⑥ 前日HYG終値が直近60営業日高値から−5%以内（金融不安レジームなら停止）
  ⑦ 撤退・最終判断は裁量（b0nchichi）。本スクリプトは判断材料の提示まで。

実行: python3 scripts/vi_signal_check.py
"""
from __future__ import annotations

import io
import warnings

warnings.filterwarnings("ignore")

import pandas as pd
import requests
import yfinance as yf

UA = {"User-Agent": "Mozilla/5.0"}
URL_VI = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv"
URL_N225 = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv"


def fetch(url: str) -> pd.DataFrame:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content), encoding="cp932")
    df.columns = ["date", "close", "open", "high", "low"][: len(df.columns)]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date")
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna()


def main() -> None:
    vi = fetch(URL_VI)
    nk = fetch(URL_N225)
    d = vi.index[-1]
    v, n = vi.loc[d], nk.loc[d]

    hyg = yf.download("HYG", period="6mo", progress=False, auto_adjust=True)
    if hasattr(hyg.columns, "levels"):
        hyg.columns = hyg.columns.get_level_values(0)
    h = hyg["Close"].dropna()
    dd60 = (h.iloc[-1] / h.rolling(60).max().iloc[-1] - 1) * 100

    c1 = v.open >= 30
    c2 = v.close > v.open
    c3 = n.close < n.open
    c6 = dd60 > -5.0
    mark = lambda b: "○" if b else "×"

    print(f"=== VI戦略シグナルチェック  最新データ日: {d.date()} ===")
    print(f"① VI始値≥30        : {mark(c1)}  (始値 {v.open:.2f})")
    print(f"② VI終値>始値       : {mark(c2)}  ({v.open:.2f} → {v.close:.2f})")
    print(f"③ 日経終値<始値     : {mark(c3)}  ({n.open:,.0f} → {n.close:,.0f})")
    print(f"⑥ クレジット平穏    : {mark(c6)}  (HYG 60d高値比 {dd60:+.2f}%, 閾値-5%)")
    # フォワードテスト方針（b0nchichi 決定, 2026-06-13）:
    #   運用は当初の3条件のみ。⑥はシャドーモード（記録のみ・運用判断に使わない）。
    #   テスト中の被弾は不可抗力として記帳。最終判断・撤退は裁量（⑦）。
    if c1 and c2 and c3:
        print("\n→ 発火（3条件成立）。引け（または翌寄り）で日経ロング、5営業日持ち切りが基本形。")
        print(f"  ⑥シャドー記録: HYG 60d高値比 {dd60:+.2f}% → {'平穏' if c6 else '金融不安レジーム（⑥なら停止していた日）'}")
        print("  ⑦ 最終判断は裁量。判断ログ（日付・判断・理由）を一行残すこと。")
    else:
        print("\n→ 発火なし。")


if __name__ == "__main__":
    main()

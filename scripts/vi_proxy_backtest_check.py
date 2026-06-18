#!/usr/bin/env python3
"""
VI日内戦略 — プロキシ版の検算（前回の +1.92% がプロキシで再現できるか）

目的:
  b0nchichi の記憶（前回は日経VI実値の寄り引けを引いた）と、かなえの過去ログ
  （前回はVIプロキシ=HV22+VRP を使い、保持データはVIと日経終値のみ）が食い違う。
  これを記憶の信用比べではなく実証で裁く。プロキシで戦略を組み、過去に出た
  中央値 +1.92% が再現する基準点・期間があるかを総当たりする。

注意（プロキシの構造的制約）:
  HV22(22日ヒストリカルボラ年率化)+VRP は日次終値から作るため「日内変動」を
  持てない。よって発火条件①を「日次の前日比上昇（恐怖が日をまたいで積み上がる）」
  で代用する。もしこれで +1.92% が再現するなら、前回の戦略は「日内」ではなく
  「日次」変動で組まれていた、という答え合わせになる。

実行: python3 scripts/vi_proxy_backtest_check.py
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf


def _flatten(d: pd.DataFrame) -> pd.DataFrame:
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    return d


def nikkei(period: str) -> pd.DataFrame:
    d = _flatten(yf.download("^N225", period=period, progress=False, auto_adjust=False))
    return d[["Open", "Close"]].dropna()


def vi_proxy(close: pd.Series, window: int = 22, vrp: float = 3.0) -> pd.Series:
    logret = np.log(close / close.shift(1))
    hv = logret.rolling(window).std() * np.sqrt(252) * 100
    return hv + vrp


def backtest(n: pd.DataFrame, vip: pd.Series, base: float, hold: int = 5) -> list[float]:
    """発火: VIプロキシ≥base かつ 前日比上昇 → その日の引けで仕込み hold営業日後。"""
    fire = (vip >= base) & (vip > vip.shift(1))
    rets = []
    closes = n["Close"].values
    fire_pos = np.where(fire.fillna(False).values)[0]
    for i in fire_pos:
        if i + hold < len(closes):
            entry = closes[i]
            exit_ = closes[i + hold]
            rets.append((exit_ - entry) / entry * 100.0)
    return rets


def main() -> None:
    print("=== VI日内戦略 プロキシ版 検算（+1.92% 再現探索）===")
    print("発火条件: VIプロキシ(HV22+VRP3%) ≥ base かつ 前日比上昇 / 保有5営業日\n")
    for period in ["2y", "3y", "5y"]:
        n = nikkei(period)
        vip = vi_proxy(n["Close"])
        print(f"--- 期間 {period}  ({n.index[0].date()}〜{n.index[-1].date()}, {len(n)}営業日) ---")
        print(f"  VIプロキシ range: {vip.min():.1f} 〜 {vip.max():.1f}  中央値 {vip.median():.1f}")
        for base in [26, 28, 30, 32, 35, 38, 40, 42, 45, 48]:
            r = backtest(n, vip, base)
            if r:
                wr = sum(x > 0 for x in r) / len(r) * 100
                med = float(np.median(r))
                mean = float(np.mean(r))
                hit = "  <<< ≈+1.92%" if abs(med - 1.92) <= 0.25 else ""
                print(f"  base={base:>2}: n={len(r):>3}  勝率{wr:4.0f}%  中央値{med:+.2f}%  平均{mean:+.2f}%{hit}")
            else:
                print(f"  base={base:>2}: 発火0")
        print()


if __name__ == "__main__":
    main()

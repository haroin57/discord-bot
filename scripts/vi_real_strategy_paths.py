#!/usr/bin/env python3
"""
VI日内戦略（実値版・ADR-0011 rev2）— 発火日デイゼロの日経経路図（0〜10営業日）

b0nchichi 指定の定番グラフ（経路図のタイムスパン延長版）:
  - 発火日（VI始値≥30 かつ VI終値>始値 かつ 日経終値<始値）をデイゼロとする
  - 横軸: 経過営業日 0〜10 / 縦軸: デイゼロ日経終値からの変化率%
  - 全経路（薄線）+ 平均（太線）+ 中央値（破線）+ ±1σ帯で分散を見せる
  - 最新のライブ発火（経路が10日未満）は赤で強調

データ: 日経公式 indexes.nikkei.co.jp ヒストリカルCSV（cp932・OHLC入り）
出力: /tmp/discord_bot_attachments/vi_real_strategy_paths.png
"""
from __future__ import annotations

import io
import warnings

warnings.filterwarnings("ignore")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

plt.rcParams["font.family"] = ["Noto Sans CJK JP", "IPAexGothic", "sans-serif"]

BASE = 30.0
HOLD_MAX = 10
UA = {"User-Agent": "Mozilla/5.0"}
URL_VI = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv"
URL_N225 = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv"
OUT = "/tmp/discord_bot_attachments/vi_real_strategy_paths.png"


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
    df = vi.join(nk, lsuffix="_vi", rsuffix="_nk", how="inner").sort_index()
    print(f"data: {df.index[0].date()} 〜 {df.index[-1].date()}  {len(df)}営業日")

    fire = (
        (df["open_vi"] >= BASE)
        & (df["close_vi"] > df["open_vi"])
        & (df["close_nk"] < df["open_nk"])
    )
    fire_days = df.index[fire]
    closes = df["close_nk"].values
    idx_map = {d: i for i, d in enumerate(df.index)}

    paths, live = [], []  # (date, np.array of pct day0..)
    for d in fire_days:
        i = idx_map[d]
        seg = closes[i : i + HOLD_MAX + 1]
        pct = (seg / seg[0] - 1.0) * 100.0
        (paths if len(pct) == HOLD_MAX + 1 else live).append((d, pct))

    print(f"発火 {len(fire_days)}回（完走 {len(paths)} / 進行中 {len(live)}）")
    mat = np.array([p for _, p in paths])
    mean, med, sd = mat.mean(0), np.median(mat, 0), mat.std(0, ddof=1)

    fig, ax = plt.subplots(figsize=(11, 7))
    x = np.arange(HOLD_MAX + 1)
    for _, p in paths:
        ax.plot(x, p, color="steelblue", alpha=0.30, lw=1.0)
    ax.fill_between(x, mean - sd, mean + sd, color="orange", alpha=0.18, label="平均±1σ")
    ax.plot(x, mean, color="darkorange", lw=2.8, label=f"平均 (day10 {mean[-1]:+.2f}%)")
    ax.plot(x, med, color="crimson", lw=2.0, ls="--", label=f"中央値 (day10 {med[-1]:+.2f}%)")
    for d, p in live:
        ax.plot(np.arange(len(p)), p, color="red", lw=2.4, marker="o", ms=4,
                label=f"進行中 {d.date()} (day{len(p)-1} {p[-1]:+.2f}%)")
    ax.axhline(0, color="gray", lw=0.8)
    ax.set_xlabel("発火日からの経過営業日")
    ax.set_ylabel("日経平均 終値変化率 %（デイゼロ比）")
    ax.set_title(
        f"VI日内戦略（実値3条件 base={BASE:.0f}） 発火{len(fire_days)}回の日経経路 0〜{HOLD_MAX}営業日\n"
        f"{df.index[0].date()}〜{df.index[-1].date()}  day5 中央値{med[5]:+.2f}% σ{sd[5]:.2f}%  "
        f"day10 中央値{med[-1]:+.2f}% σ{sd[-1]:.2f}%"
    )
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT, dpi=130)
    print(f"saved: {OUT}")
    for k in (1, 3, 5, 10):
        wr = (mat[:, k] > 0).mean() * 100
        print(f"  day{k:>2}: 中央値{np.median(mat[:, k]):+.2f}%  平均{mat[:, k].mean():+.2f}%  σ{mat[:, k].std(ddof=1):.2f}%  勝率{wr:.0f}%")


if __name__ == "__main__":
    main()

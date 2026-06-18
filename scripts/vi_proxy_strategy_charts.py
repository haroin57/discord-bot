#!/usr/bin/env python3
"""
VI日次平均回帰戦略 可視化（b0nchichi VI戦略・確定版 / ADR-0011）

図1: 散布図  横軸=日経終値, 縦軸=VIプロキシ(HV22+VRP3%), 半年分
図2: 経路図  base=30 発火回の day0(引けで仕込み) からの日経平均変化(%),
            81本の経路 + 平均 + ±1σ（平均と分散を一望）

戦略確定版（2026-06-13 検算で確定）:
  発火: VIプロキシ ≥ 30 かつ 前日比上昇（日次で恐怖が積み上がる）
  執行: その日の引けで日経ロング, 5営業日保有
  実績: 5年 n=81, 勝率70%, 中央値 +1.92%（プロキシ上）

実行: python3 scripts/vi_proxy_strategy_charts.py
"""
import warnings; warnings.filterwarnings("ignore")
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
try:
    import japanize_matplotlib  # noqa: F401
except Exception:
    pass

OUT = Path("/tmp/discord_bot_attachments")
OUT.mkdir(parents=True, exist_ok=True)
BASE = 30.0
HOLD = 5


def flatten(d):
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    return d


def vi_proxy(close, w=22, vrp=3.0):
    lr = np.log(close / close.shift(1))
    return lr.rolling(w).std() * np.sqrt(252) * 100 + vrp


n5 = flatten(yf.download("^N225", period="5y", progress=False, auto_adjust=False))[["Close"]].dropna()
vip5 = vi_proxy(n5["Close"])

# === 図1: 経時グラフ（日経終値×VI を時間順に線で結んだ軌跡, 半年）===
from matplotlib.collections import LineCollection
half = n5.iloc[-126:]
vihalf = vip5.loc[half.index]
xv = half["Close"].values
yv = vihalf.values
fig, ax = plt.subplots(figsize=(10, 7), dpi=120)
points = np.array([xv, yv]).T.reshape(-1, 1, 2)
segments = np.concatenate([points[:-1], points[1:]], axis=1)
lc = LineCollection(segments, cmap="viridis", linewidth=1.6, alpha=0.85)
lc.set_array(np.arange(len(segments)))
ax.add_collection(lc)
ax.scatter(xv, yv, c=range(len(xv)), cmap="viridis", s=11, alpha=0.45, edgecolor="none")
ax.plot(xv[0], yv[0], "o", color="blue", ms=13, markeredgecolor="white", markeredgewidth=1.5,
        label=f"開始 {half.index[0].date()}")
ax.plot(xv[-1], yv[-1], "s", color="red", ms=13, markeredgecolor="white", markeredgewidth=1.5,
        label=f"最新 {half.index[-1].date()}")
ax.axhline(BASE, color="orange", ls="--", lw=1.2, label=f"基準点 VI={BASE:.0f}")
# 半年内で発火条件(VIプロキシ≥30 かつ 前日比上昇)を満たした日を赤丸で強調
fire_all = (vip5 >= BASE) & (vip5 > vip5.shift(1))
fire_k = np.where(fire_all.loc[half.index].fillna(False).values)[0]
ax.scatter(xv[fire_k], yv[fire_k], s=150, facecolor="none", edgecolor="red",
           linewidth=2.0, zorder=6, label=f"発火日 {len(fire_k)}回")
print(f"半年内の発火日 ({len(fire_k)}回): "
      + ", ".join(half.index[k].strftime("%m/%d") for k in fire_k))
# 5営業日ごとに日付ラベル（軌跡上のどの点がいつかを読めるように）
for k in range(0, len(xv), 5):
    ax.plot(xv[k], yv[k], "o", color="black", ms=4, alpha=0.75, zorder=4)
    ax.annotate(half.index[k].strftime("%m/%d"), xy=(xv[k], yv[k]),
                xytext=(4, 4), textcoords="offset points", fontsize=7,
                color="black", alpha=0.9, zorder=5)
ax.set_xlim(xv.min() * 0.99, xv.max() * 1.01)
ax.set_ylim(yv.min() * 0.9, yv.max() * 1.05)
ax.set_xlabel("日経平均 終値")
ax.set_ylabel("VIプロキシ (HV22 + VRP3%)")
ax.set_title(f"日経終値 × VI 経時グラフ（半年 {half.index[0].date()}〜{half.index[-1].date()}, 色=時間経過）")
sm = plt.cm.ScalarMappable(cmap="viridis", norm=plt.Normalize(0, len(xv)))
plt.colorbar(sm, ax=ax, label="時間経過 →")
ax.legend()
fig.tight_layout()
fig.savefig(OUT / "vi_scatter_half.png")
plt.close(fig)

# === 図2: 発火回の経路（day0からの日経変化）===
fire = (vip5 >= BASE) & (vip5 > vip5.shift(1))
pos = np.where(fire.fillna(False).values)[0]
closes = n5["Close"].values
paths = []
for i in pos:
    if i + HOLD < len(closes):
        b = closes[i]
        paths.append([(closes[i + k] - b) / b * 100.0 for k in range(HOLD + 1)])
paths = np.array(paths)
mean_p, std_p = paths.mean(0), paths.std(0)
x = np.arange(HOLD + 1)

fig, ax = plt.subplots(figsize=(10, 7), dpi=120)
for p in paths:
    ax.plot(x, p, color="gray", alpha=0.13, lw=0.8)
ax.fill_between(x, mean_p - std_p, mean_p + std_p, color="#4477aa", alpha=0.25,
                label="±1σ (分散)")
ax.plot(x, mean_p, color="#cc3311", lw=2.6, marker="o",
        label=f"平均 (day5 = {mean_p[-1]:+.2f}%)")
ax.axhline(0, color="black", lw=0.8)
ax.set_xlabel("エントリーからの営業日（day0 = 引けで仕込み）")
ax.set_ylabel("day0 からの日経平均 変化 (%)")
ax.set_title(f"VI日次戦略 発火{len(paths)}回の経路  "
             f"中央値day5={np.median(paths[:, -1]):+.2f}%  勝率{(paths[:, -1] > 0).mean()*100:.0f}%")
ax.legend()
fig.tight_layout()
fig.savefig(OUT / "vi_strategy_paths.png")
plt.close(fig)

print(f"発火回数 n = {len(paths)}")
print(f"day5  平均 {mean_p[-1]:+.2f}%  中央値 {np.median(paths[:, -1]):+.2f}%  σ {std_p[-1]:.2f}%")
print(f"勝率 {(paths[:, -1] > 0).mean()*100:.0f}%")
print(f"各日 平均: " + "  ".join(f"d{k}={mean_p[k]:+.2f}%" for k in range(HOLD + 1)))
print(f"各日 σ   : " + "  ".join(f"d{k}={std_p[k]:.2f}%" for k in range(HOLD + 1)))
print(f"出力: {OUT}/vi_scatter_half.png")
print(f"出力: {OUT}/vi_strategy_paths.png")

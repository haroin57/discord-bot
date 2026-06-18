#!/usr/bin/env python3
"""
VI日内変動 平均回帰戦略 バックテスト（b0nchichi VI日内戦略 / ADR候補）

戦略定義（b0nchichi が 2026-06-13 に明確化）:
  (1) 恐怖指標の日内変動が上向き = ザラ場で恐怖が積み上がる日
  (2) かつベースが高水準（日経VI30相当）から、さらに上がっていく稀な日
  (3) その日の引けで日経を仕込む（恐怖ピークの逆張りロング）
  (4) 5営業日後の勝率・リターンで評価する

―― データの限界（最重要・読み飛ばし厳禁）――
  * 日経VIの実値は無料APIで取得不可（^JNIV 等すべて空、J-Quants キーなし）。
  * 代理として US VIX(^VIX) を使用。日経VIとの実証相関は 0.75-0.85。別物である。
  * 水準が異なるため発火閾値は暫定（日経VI30 ≈ US VIX 20 と仮置き）。
  * 時差: US VIX の D 日セッションは日本の D+1 日の「前夜」にあたる。
    よって VIX が D 日に発火 → 日経は次営業日 (D+1) の引けで仕込む。
    これでルックアヘッド（未来情報の混入）を回避している。
  * 日経VI実値が入手できたら fetch_fear_index() だけ差し替えれば、
    同じロジックで「本採点」に置き換わる（速報→確定の二段運用）。

これは代理データによる「暫定成績表」である。US VIX を日経VIに読み替えた
近似であり、+X% という数字を実弾の唯一の根拠にしてはならない。

実行: python3 scripts/vi_intraday_backtest.py
出力:
  - /tmp/discord_bot_attachments/vi_intraday_backtest_trades.csv  (全トレード)
  - /tmp/discord_bot_attachments/vi_intraday_backtest_dist.png    (リターン分布)
  - stdout にサマリ（勝率 / 中央値 / 曜日別 / 閾値感度）
"""
from __future__ import annotations

import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf

OUT_DIR = Path("/tmp/discord_bot_attachments")
WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _flatten(d: pd.DataFrame) -> pd.DataFrame:
    """yfinance 1.2 / pandas 3.0 の MultiIndex 列を平坦化。"""
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    return d


def fetch_fear_index(period: str = "2y") -> pd.DataFrame:
    """恐怖指標の日次 OHLC。

    現状は US VIX 代理。日経VI実値が手に入ったら、ここを
    JPX/J-Quants 由来の日経VI OHLC に差し替えるだけで本採点になる。
    """
    d = _flatten(yf.download("^VIX", period=period, progress=False, auto_adjust=False))
    return d[["Open", "High", "Low", "Close"]].dropna()


def fetch_nikkei(period: str = "2y") -> pd.DataFrame:
    d = _flatten(yf.download("^N225", period=period, progress=False, auto_adjust=False))
    return d[["Open", "High", "Low", "Close"]].dropna()


def find_signals(fear: pd.DataFrame, base_threshold: float) -> pd.DatetimeIndex:
    """発火日: (1)始値→終値が上昇(日内で恐怖積み上がり) かつ (2)始値≥閾値。"""
    intraday_up = fear["Close"] > fear["Open"]
    base_high = fear["Open"] >= base_threshold
    return fear.index[intraday_up & base_high]


def run_backtest(
    signal_dates: pd.DatetimeIndex,
    nikkei: pd.DataFrame,
    hold_days: int = 5,
) -> pd.DataFrame:
    """各発火日について、次の日経営業日の引けで仕込み hold_days 営業日後に手仕舞い。"""
    ndates = nikkei.index
    rows = []
    for d in signal_dates:
        future = ndates[ndates > d]  # VIX発火日より後の最初の日経営業日 = エントリー
        if len(future) < hold_days + 1:
            continue
        entry_date = future[0]
        exit_date = future[hold_days]
        entry = float(nikkei.loc[entry_date, "Close"])
        exit_ = float(nikkei.loc[exit_date, "Close"])
        ret = (exit_ - entry) / entry * 100.0
        rows.append(
            {
                "signal_date": d.date(),
                "entry_date": entry_date.date(),
                "entry_wday": WEEKDAY_JA[entry_date.weekday()],
                "entry_px": round(entry, 1),
                "exit_date": exit_date.date(),
                "exit_px": round(exit_, 1),
                "ret_pct": round(ret, 3),
                "win": ret > 0,
            }
        )
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame) -> None:
    n = len(trades)
    if n == 0:
        print("トレード0件。閾値かデータ期間を見直してください。")
        return
    wr = trades["win"].mean() * 100
    med = trades["ret_pct"].median()
    mean = trades["ret_pct"].mean()
    print(f"  サンプル数      : {n} トレード")
    print(f"  勝率(5営業日後) : {wr:.1f}%")
    print(f"  中央値リターン  : {med:+.2f}%")
    print(f"  平均リターン    : {mean:+.2f}%")
    print(f"  最大 / 最小     : {trades['ret_pct'].max():+.2f}% / {trades['ret_pct'].min():+.2f}%")
    print("  曜日別（エントリー日）:")
    for wd, g in trades.groupby("entry_wday", sort=False):
        print(f"    {wd}: {len(g):2d}件  勝率{g['win'].mean()*100:4.0f}%  中央値{g['ret_pct'].median():+.2f}%")


def sensitivity(fear: pd.DataFrame, nikkei: pd.DataFrame, hold_days: int = 5) -> None:
    """閾値を振ってオーバーフィット耐性を見る。"""
    print("  閾値感度（VIX始値の下限を変えたとき）:")
    for th in [18, 20, 22, 24]:
        sig = find_signals(fear, th)
        tr = run_backtest(sig, nikkei, hold_days)
        if len(tr):
            print(f"    VIX≥{th}: {len(tr):2d}件  勝率{tr['win'].mean()*100:4.0f}%  中央値{tr['ret_pct'].median():+.2f}%")
        else:
            print(f"    VIX≥{th}: 0件")


def render_distribution(trades: pd.DataFrame, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        import japanize_matplotlib  # noqa: F401
    except Exception:
        pass
    fig, ax = plt.subplots(figsize=(9, 5), dpi=120)
    rets = trades["ret_pct"].values
    ax.hist(rets, bins=15, color="#4477aa", alpha=0.8, edgecolor="white")
    ax.axvline(0, color="gray", ls="--", lw=1)
    ax.axvline(np.median(rets), color="red", ls="-", lw=1.5,
               label=f"中央値 {np.median(rets):+.2f}%")
    ax.set_xlabel("5営業日後リターン (%)")
    ax.set_ylabel("頻度")
    ax.set_title(f"VI日内戦略(US VIX代理) リターン分布  n={len(trades)} 勝率{trades['win'].mean()*100:.0f}%")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=== VI日内変動 平均回帰戦略 バックテスト（US VIX 代理）===")
    fear = fetch_fear_index()
    nikkei = fetch_nikkei()
    print(f"恐怖指標(US VIX): {len(fear)}日  日経225: {len(nikkei)}日  "
          f"期間 {fear.index[0].date()}〜{fear.index[-1].date()}\n")

    BASE = 20.0  # 日経VI30相当の US VIX 暫定水準
    HOLD = 5
    signals = find_signals(fear, BASE)
    trades = run_backtest(signals, nikkei, HOLD)

    print(f"[発火条件] VIX始値≥{BASE} かつ 始値→終値が上昇 / 保有 {HOLD}営業日\n")
    summarize(trades)
    print()
    sensitivity(fear, nikkei, HOLD)

    if len(trades):
        csv_path = OUT_DIR / "vi_intraday_backtest_trades.csv"
        png_path = OUT_DIR / "vi_intraday_backtest_dist.png"
        trades.to_csv(csv_path, index=False)
        render_distribution(trades, png_path)
        print(f"\n出力: {csv_path}")
        print(f"出力: {png_path}")


if __name__ == "__main__":
    main()

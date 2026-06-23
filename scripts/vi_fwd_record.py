#!/usr/bin/env python3
"""VI戦略 fwd記録スクリプト (ADR-0026)

発火サンプル (.state/vi_fwd_records.json) の fwd0〜settle_fwd を、
VI・日経 (日経公式CSV) ・1570 (yfinance) の終値で埋める。

毎営業日18:00に vi_signal_scheduler から呼ばれる想定。
手動実行も可: python scripts/vi_fwd_record.py [--dry-run]

設計 (ADR-0026):
  - JSON正本 (.state/vi_fwd_records.json)。.md は表示用派生物。
  - vi_signal_alert.py には相乗りしない (関心分離)。fetch_csv は複製。
  - 冪等: 既に埋まった fwd は再取得しない。未来日・全欠損日はスキップ。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

import jpholiday
import pandas as pd
import requests
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = REPO_ROOT / ".state" / "vi_fwd_records.json"

UA = {"User-Agent": "Mozilla/5.0"}
URL_VI = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv"
URL_N225 = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv"
ETF_TICKER = "1570.T"


# fetch_csv: vi_signal_alert.py から複製 (ADR-0026, import副作用回避のため)
def fetch_csv(url: str) -> pd.DataFrame:
    """日経公式CSV (cp932) を DataFrame に。index=date, cols=close/open/high/low。"""
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content), encoding="cp932")
    df.columns = ["date", "close", "open", "high", "low"][: len(df.columns)]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date")
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna()


def fetch_etf_close(ticker: str) -> pd.Series:
    """ETF (1570) の終値 Series を返す。index は tz除去+normalize して日付に揃える。"""
    df = yf.download(ticker, period="3mo", progress=False, auto_adjust=True)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    s = df["Close"].dropna()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    return s


def is_business_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def nth_business_day(start: date, n: int) -> date:
    """start を fwd0 として n営業日後の日付を返す。"""
    d = start
    count = 0
    while count < n:
        d = d + timedelta(days=1)
        if is_business_day(d):
            count += 1
    return d


def close_on(df_or_series, target: date) -> float | None:
    """DataFrame(またはSeries)から target日の close を引く。無ければ None。"""
    ts = pd.Timestamp(target)
    if ts not in df_or_series.index:
        return None
    val = df_or_series.loc[ts]
    if isinstance(val, pd.DataFrame):  # 同日複数行 (異常) は先頭
        val = val.iloc[0]
    if isinstance(val, pd.Series):  # DataFrame行 → close列
        val = val["close"]
    return round(float(val), 2)


def main() -> int:
    parser = argparse.ArgumentParser(description="VI戦略 fwd記録 (ADR-0026)")
    parser.add_argument("--dry-run", action="store_true", help="保存せず結果表示のみ")
    args = parser.parse_args()

    if not STATE_FILE.exists():
        print(f"ERROR: {STATE_FILE} が無い", file=sys.stderr)
        return 1

    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    settle_fwd = int(data.get("settle_fwd", 5))
    samples = data.get("samples", [])
    active = [s for s in samples if s.get("status") == "active"]
    if not active:
        print("アクティブな発火サンプル無し、終了")
        return 0

    # データ取得は1回だけ (全サンプル共通)
    vi = fetch_csv(URL_VI)
    nk = fetch_csv(URL_N225)
    etf = fetch_etf_close(ETF_TICKER)
    today = date.today()

    updated = 0
    for s in active:
        fire = datetime.strptime(s["fire_date"], "%Y-%m-%d").date()
        fwd = s.setdefault("fwd", {})
        for n in range(0, settle_fwd + 1):
            key = str(n)
            if key in fwd:  # 冪等: 既存は触らない
                continue
            fwd_date = nth_business_day(fire, n)
            if fwd_date > today:  # 未来はスキップ
                continue
            vi_c = close_on(vi, fwd_date)
            nk_c = close_on(nk, fwd_date)
            etf_c = close_on(etf, fwd_date)
            if vi_c is None and nk_c is None and etf_c is None:
                print(f"  {s['fire_date']} fwd{n} ({fwd_date}): 全欠損(未反映/休場), skip")
                continue
            fwd[key] = {
                "date": str(fwd_date),
                "vi_close": vi_c,
                "nk_close": nk_c,
                "etf_close": etf_c,
            }
            updated += 1
            print(f"  {s['fire_date']} fwd{n} ({fwd_date}): VI={vi_c} NK={nk_c} 1570={etf_c}")

        # settle_fwd まで揃ったら closed
        if all(str(n) in fwd for n in range(0, settle_fwd + 1)):
            s["status"] = "closed"
            print(f"  {s['fire_date']}: fwd0〜{settle_fwd} 全充足 → status=closed")

    if updated == 0:
        print("更新なし（全fwd既充足 or 未来/未反映）")
        return 0

    if args.dry_run:
        print(f"\n[DRY-RUN] {updated}件のfwdを更新予定（保存せず）")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n✅ {updated}件のfwdを記録、{STATE_FILE.name} 更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())

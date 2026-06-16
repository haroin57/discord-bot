#!/usr/bin/env python3
"""VI発火通知システム (Phase 1: 確報通知)

既存 scripts/vi_signal_check.py の判定ロジックを流用し、
発火確定時に Discord + Resend メールで通知する。

cron実行 (例):
  30 16 * * 1-5  cd /home/ubuntu/discord-bot && /usr/bin/python3 scripts/vi_signal_alert.py final

オプション:
  --force        : 発火条件を満たさなくてもテスト送信する
  --stage STAGE  : ステージ識別子 (morning | afternoon | final), デフォルト final
  --dry-run      : 実際の送信はせず、整形結果だけ表示する

設計仕様 (b0nchichi 確定, 2026-06-15):
  - メンション対象: b0nchichi (1037285234200297522) のみ
  - メール送信先: bnchichi7@gmail.com (Gmail→ezweb携帯転送)
  - 通知タイミング: 11:30前場引け / 14:30後場前半 / 16:30引け後確報 の3段階
    (Phase 1ではfinalだけ実装、morning/afternoonはPhase 2で追加)
  - 条件崩れでサイクル中止通知 (Phase 2で実装)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import warnings
from datetime import date, datetime
from pathlib import Path

warnings.filterwarnings("ignore")

# .env 読み込み
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

import jpholiday
import pandas as pd
import requests
import yfinance as yf

# =============================================================================
# 定数・設定
# =============================================================================

UA = {"User-Agent": "Mozilla/5.0"}
URL_VI = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv"
URL_N225 = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "VI Alert <onboarding@resend.dev>")
RESEND_TO = os.environ.get("RESEND_TO_EMAIL", "bnchichi7@gmail.com")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get(
    "VI_ALERT_DISCORD_CHANNEL_ID", "1488369795807055964"
)
DISCORD_MENTION_USER_ID = "1037285234200297522"  # b0nchichi

STATE_DIR = REPO_ROOT / ".state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "vi_signal_state.json"

# =============================================================================
# データ取得
# =============================================================================


def fetch_csv(url: str) -> pd.DataFrame:
    """日経公式CSV (cp932) を DataFrame に。"""
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content), encoding="cp932")
    df.columns = ["date", "close", "open", "high", "low"][: len(df.columns)]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date")
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna()


def fetch_hyg_dd60() -> float:
    """HYG 60営業日高値からのドローダウン (%) を取得する。"""
    hyg = yf.download("HYG", period="6mo", progress=False, auto_adjust=True)
    if hasattr(hyg.columns, "levels"):
        hyg.columns = hyg.columns.get_level_values(0)
    h = hyg["Close"].dropna()
    dd60 = (h.iloc[-1] / h.rolling(60).max().iloc[-1] - 1) * 100
    return float(dd60)


# =============================================================================
# 判定ロジック
# =============================================================================


def evaluate_signal() -> dict:
    """3条件 + ⑥クレジット平穏 を最新確報データで判定する。"""
    vi = fetch_csv(URL_VI)
    nk = fetch_csv(URL_N225)
    d = vi.index[-1]
    v, n = vi.loc[d], nk.loc[d]
    dd60 = fetch_hyg_dd60()

    c1 = bool(v.open >= 30)
    c2 = bool(v.close > v.open)
    c3 = bool(n.close < n.open)
    c6 = bool(dd60 > -5.0)
    fire = c1 and c2 and c3

    return {
        "data_date": str(d.date()),
        "vi_open": float(v.open),
        "vi_high": float(v.high),
        "vi_low": float(v.low),
        "vi_close": float(v.close),
        "nk_open": float(n.open),
        "nk_high": float(n.high),
        "nk_low": float(n.low),
        "nk_close": float(n.close),
        "hyg_dd60": dd60,
        "c1": c1,
        "c2": c2,
        "c3": c3,
        "c6": c6,
        "fire": fire,
    }


# =============================================================================
# 営業日判定
# =============================================================================


def is_business_day(d: date) -> bool:
    if d.weekday() >= 5:  # 土日
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


# =============================================================================
# 状態管理
# =============================================================================


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# =============================================================================
# 通知整形
# =============================================================================


def format_alert(sig: dict, stage: str, force: bool = False) -> tuple[str, str, str]:
    """戻り値: (Discord本文, メール件名, メール本文)"""
    d = sig["data_date"]
    mark = lambda b: "○" if b else "×"

    if sig["fire"]:
        header_d = f"🚨 **VI発火確定** ({d}, {stage_label(stage)})"
        header_e = f"【VI発火確定】{d} {stage_label(stage)}"
    elif force:
        header_d = f"🧪 **【テスト送信】VI判定** ({d}, {stage_label(stage)})"
        header_e = f"【テスト】VI判定 {d} {stage_label(stage)}"
    else:
        header_d = f"✅ **VI判定: 発火なし** ({d}, {stage_label(stage)})"
        header_e = f"【発火なし】{d} {stage_label(stage)}"

    body_common = (
        f"**3条件判定 (最新確報データ: {d})**\n"
        f"① VI始値≥30        : {mark(sig['c1'])}  (始値 {sig['vi_open']:.2f})\n"
        f"② VI終値>始値       : {mark(sig['c2'])}  ({sig['vi_open']:.2f} → {sig['vi_close']:.2f})\n"
        f"③ 日経終値<始値     : {mark(sig['c3'])}  ({sig['nk_open']:,.0f} → {sig['nk_close']:,.0f})\n"
        f"⑥ クレジット平穏    : {mark(sig['c6'])}  (HYG 60d高値比 {sig['hyg_dd60']:+.2f}%, 閾値-5%)\n"
    )

    if sig["fire"]:
        action = (
            "\n**→ エントリー判断材料**\n"
            "・引け or 翌寄りで 1570 (日経レバ2倍ETF) ロング\n"
            "・5営業日鬼ホールドが基本形 (バックテスト中央値 +3.08%, 勝率79%)\n"
            "・⑦最終判断は裁量、判断ログを残してください\n"
            f"・⑥シャドー記録: {'平穏' if sig['c6'] else '金融不安レジーム (⑥なら停止していた日)'}\n"
        )
    else:
        action = "\n→ エントリー条件は揃っていません。\n"

    discord_content = (
        f"<@{DISCORD_MENTION_USER_ID}>\n{header_d}\n\n{body_common}{action}"
    )
    email_subject = header_e
    email_body = (
        f"{header_e}\n\n{body_common}{action}\n"
        f"-- \nVI発火通知システム (送信元: かなえ bot)\n"
    )
    return discord_content, email_subject, email_body


def stage_label(stage: str) -> str:
    return {
        "morning": "前場引け後",
        "afternoon": "後場前半",
        "final": "引け後確報",
    }.get(stage, stage)


# =============================================================================
# 通知送信
# =============================================================================


def send_email(subject: str, body: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[DRY-RUN EMAIL] Subject: {subject}")
        print(f"[DRY-RUN EMAIL] Body:\n{body}")
        return True
    if not RESEND_API_KEY:
        print("ERROR: RESEND_API_KEY not set in .env", file=sys.stderr)
        return False
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": RESEND_FROM,
            "to": [RESEND_TO],
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    if resp.status_code in (200, 201, 202):
        print(f"✅ Email sent: status={resp.status_code}, id={resp.json().get('id')}")
        return True
    print(
        f"❌ Email failed: status={resp.status_code}, body={resp.text}",
        file=sys.stderr,
    )
    return False


def send_discord(content: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[DRY-RUN DISCORD] Channel: {DISCORD_CHANNEL_ID}")
        print(f"[DRY-RUN DISCORD] Content:\n{content}")
        return True
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set in .env", file=sys.stderr)
        return False
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "content": content,
            "allowed_mentions": {"users": [DISCORD_MENTION_USER_ID]},
        },
        timeout=15,
    )
    if resp.status_code in (200, 201):
        print(f"✅ Discord posted: status={resp.status_code}")
        return True
    print(
        f"❌ Discord failed: status={resp.status_code}, body={resp.text}",
        file=sys.stderr,
    )
    return False


# =============================================================================
# メイン
# =============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="VI発火通知システム (Phase 1)")
    parser.add_argument(
        "--stage", default="final", choices=["morning", "afternoon", "final"]
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="発火条件を満たさなくてもテスト送信する",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実際の送信はせず、整形結果だけ表示する",
    )
    parser.add_argument(
        "--ignore-business-day",
        action="store_true",
        help="営業日チェックを無視する (テスト用)",
    )
    args = parser.parse_args()

    today = date.today()
    if not args.ignore_business_day and not is_business_day(today):
        print(f"非営業日 {today}、終了")
        return 0

    sig = evaluate_signal()

    # データ日付ガード: 日経公式CSVの当日確報反映は16時前後で変動するため、
    # 当日データが反映されていないときに前日データで誤通知しないよう守る。
    # --force / --ignore-business-day はテスト用なので無視する。
    data_date_obj = datetime.strptime(sig["data_date"], "%Y-%m-%d").date()
    if (
        data_date_obj < today
        and not args.force
        and not args.ignore_business_day
    ):
        print(
            f"WARN: CSV最新データ日 {sig['data_date']} < 今日 {today}、"
            f"当日データ未反映のため通知スキップ"
        )
        return 0

    state = load_state()
    today_key = str(today)
    state.setdefault(today_key, {})

    # 同日同stage の重複通知防止 (force/dry-runは除外)
    notified_key = f"notified_{args.stage}"
    if (
        not args.force
        and not args.dry_run
        and state[today_key].get(notified_key)
    ):
        print(f"{args.stage} 既に通知済み、スキップ")
        return 0

    should_notify = sig["fire"] or args.force
    if not should_notify:
        print(f"発火なし ({args.stage}, {sig['data_date']})、通知スキップ")
        return 0

    d_content, e_subject, e_body = format_alert(sig, args.stage, force=args.force)

    ok_email = send_email(e_subject, e_body, dry_run=args.dry_run)
    ok_discord = send_discord(d_content, dry_run=args.dry_run)

    if ok_email and ok_discord:
        if not args.dry_run and not args.force:
            state[today_key][notified_key] = True
            state[today_key][f"fire_{args.stage}"] = sig["fire"]
            state[today_key][f"data_date_{args.stage}"] = sig["data_date"]
            save_state(state)
        print(f"\n✅ 通知完了 ({args.stage})")
        return 0
    print(
        f"\n❌ 通知失敗: email={ok_email}, discord={ok_discord}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

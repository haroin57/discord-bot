#!/usr/bin/env python3
"""Resend 到達テスト

bnchichi7@gmail.com 宛にテストメールを送って、
お父さんの携帯（ezweb 転送）まで届くかを確認する。

VI発火通知システムの最初のスモークテスト。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# .env 読み込み (python-dotenv)
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import requests

API_KEY = os.environ.get("RESEND_API_KEY")
if not API_KEY:
    print("ERROR: RESEND_API_KEY not set in .env", file=sys.stderr)
    sys.exit(1)

FROM = os.environ.get("RESEND_FROM_EMAIL", "VI Alert <onboarding@resend.dev>")
TO = os.environ.get("RESEND_TO_EMAIL", "bnchichi7@gmail.com")

SUBJECT = "【テスト】VI発火通知システム 到達確認"
TEXT = (
    "これは Resend 経由のテストメールです。\n"
    "\n"
    "このメールが bnchichi7@gmail.com に届き、転送設定で\n"
    "ezweb 携帯まで到達したら、VI発火通知システムの本実装に進みます。\n"
    "\n"
    "本番では以下のタイミングで通知が来ます:\n"
    "  - 11:30 前場引け後 1次判定\n"
    "  - 14:30 後場前半 中間維持判定\n"
    "  - 16:00 引け後 確報判定\n"
    "\n"
    "途中で発火条件が崩れたらサイクル中止通知が入ります。\n"
    "\n"
    "— かなえ\n"
)

resp = requests.post(
    "https://api.resend.com/emails",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    json={"from": FROM, "to": [TO], "subject": SUBJECT, "text": TEXT},
    timeout=15,
)

print(f"Status: {resp.status_code}")
print(f"Body  : {resp.text}")

if resp.status_code in (200, 201, 202):
    print()
    print("✅ Resend API はメールを受領しました (送信成功)")
    print(f"   宛先: {TO}")
    print("   → お父さんの携帯まで届いたか確認してください")
else:
    print()
    print("❌ 送信失敗")
    sys.exit(1)

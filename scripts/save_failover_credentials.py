"""現在の Claude credentials を failover アカウントとして退避する。

使い方（旧アカウントで `claude /login` した直後に実行する）:
    .venv/bin/python scripts/save_failover_credentials.py          # 確認プロンプトあり
    .venv/bin/python scripts/save_failover_credentials.py --yes    # 確認スキップ

やること:
  1. ~/.claude/.credentials.json（現在ログイン中のアカウント）を
     ~/.claude/.credentials-failover.json へコピー（0600）
  2. ~/.claude.json から userID / oauthAccount を
     ~/.claude/.account-failover.json へ抽出（0600、metadata の accountUuid 用）
  3. どのアカウントを退避したか（uuid 先頭8字と email）を表示

安全装置:
  - 退避しようとしているアカウントの uuid / email を表示して確認を取る
  - 新アカウント（primary にすべき方）と分かっている uuid なら強く警告
  - token の中身は表示しない

退避後は、**/logout せずに** そのまま新アカウントで `/login` し直すこと
（/logout はサーバー側で現在の grant を revoke するため、退避したばかりの
refresh token が即死する — 2026-07-02 に実地で踏んだ）。その後
`scripts/verify_failover.py` で両アカウントの分離を確認する。
"""

import argparse
import json
import os
import shutil
import sys

CREDENTIALS = os.path.expanduser("~/.claude/.credentials.json")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
FAILOVER_CREDS = os.path.expanduser("~/.claude/.credentials-failover.json")
FAILOVER_SIDECAR = os.path.expanduser("~/.claude/.account-failover.json")

# primary（新契約）アカウントの uuid 先頭8字。これを退避しようとしたら警告する。
# アカウント構成を変えたら更新すること。
PRIMARY_UUID8 = "232b77ec"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="確認プロンプトをスキップ")
    args = ap.parse_args()

    # 現在のアカウント identity を読む
    try:
        with open(CLAUDE_JSON, encoding="utf-8") as f:
            cj = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"❌ {CLAUDE_JSON} が読めません: {exc}")
        return 1
    acct = cj.get("oauthAccount") or {}
    uuid8 = (acct.get("accountUuid") or "")[:8]
    email = acct.get("emailAddress") or acct.get("email") or "?"
    if not uuid8:
        print("❌ ~/.claude.json に oauthAccount が無い（未ログイン？ /login してから実行）")
        return 1

    try:
        with open(CREDENTIALS, encoding="utf-8") as f:
            creds = json.load(f)
        if not (creds.get("claudeAiOauth") or {}).get("accessToken"):
            print(f"❌ {CREDENTIALS} に accessToken が無い（未ログイン？）")
            return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"❌ {CREDENTIALS} が読めません: {exc}")
        return 1

    print(f"退避対象: uuid={uuid8}… email={email}")
    if uuid8 == PRIMARY_UUID8:
        print("⚠️⚠️ これは primary（新契約）アカウントです。failover として退避するのは"
              "たぶん間違いです（退避すべきは旧アカウント）。")
    if not args.yes:
        answer = input("このアカウントを failover として退避しますか？ [y/N]: ").strip().lower()
        if answer != "y":
            print("中止しました")
            return 1

    # 1. credentials コピー（0600）
    shutil.copyfile(CREDENTIALS, FAILOVER_CREDS)
    os.chmod(FAILOVER_CREDS, 0o600)
    print(f"✅ credentials → {FAILOVER_CREDS}")

    # 2. identity サイドカー（metadata の accountUuid 用）
    sidecar = {"userID": cj.get("userID", ""), "oauthAccount": acct}
    fd = os.open(FAILOVER_SIDECAR, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(sidecar, f)
    os.chmod(FAILOVER_SIDECAR, 0o600)
    print(f"✅ account identity → {FAILOVER_SIDECAR}")

    print()
    print("次の手順:")
    print("  1. claude で **そのまま** 新アカウント（primary）で /login し直す")
    print("     ⚠️ /logout は禁止 — 退避した旧アカウントの token が revoke される")
    print("  2. .venv/bin/python scripts/verify_failover.py で両アカウントの分離を確認")
    print("  3. bot を起動（sudo systemctl start discord-bot）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

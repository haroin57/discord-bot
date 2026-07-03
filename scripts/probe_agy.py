"""agy (Antigravity CLI) の疎通確認 — SecondOpinion に組み込む前の gate。

前提: サーバーに agy をインストールし、AI Pro/Ultra アカウントで OAuth ログイン済み
（`agy -i` 等）。**GEMINI_API_KEY / ANTIGRAVITY_API_KEY を env に置かないこと**
（置くと OAuth をバイパスして従量課金になる）。

実行: .venv/bin/python scripts/probe_agy.py

やること:
  1. agy バイナリの存在と --version
  2. env に API キーが残っていないかチェック（残っていたら警告）
  3. `agy -p "1+1は？一言で"` を OAuth 枠で実行 → stdout / transcript から回答取得
  4. transcript の実パスを走査して表示（AGY_TRANSCRIPT_ROOT の実測確定用）
"""

import asyncio
import os
import shutil
import sys

sys.path.insert(0, "/home/ubuntu/discord-bot")

from config import AGY_BIN, AGY_TRANSCRIPT_ROOT  # noqa: E402
from services.claude_api.agy_runner import (  # noqa: E402
    _STRIP_ENV_KEYS,
    ask_agy,
    is_agy_available,
)


async def main() -> int:
    print(f"AGY_BIN={AGY_BIN!r} which={shutil.which(AGY_BIN)!r}")
    if not shutil.which(AGY_BIN):
        print("❌ agy が PATH に無い。インストールして OAuth ログインしてください。")
        return 1

    leaked = [k for k in _STRIP_ENV_KEYS if os.environ.get(k)]
    if leaked:
        print(f"⚠️ env に {leaked} が設定されています。agy が OAuth をバイパスして"
              "従量課金に流れる恐れ（runner は自前で除去するので実害は無いが、"
              ".env からは消すことを推奨）")

    avail = await is_agy_available()
    print(f"is_agy_available={avail}")
    if not avail:
        print("❌ agy --version が失敗。認証/インストールを確認してください。")
        return 1

    print("--- agy -p '1+1は？一言で' 実行中（OAuth 枠）… ---")
    try:
        ans = await ask_agy("1+1は？一言で。ファイル操作やコマンド実行はしないで。",
                            timeout_s=120)
    except Exception as exc:
        print(f"❌ ask_agy 失敗: {type(exc).__name__}: {exc}")
        _dump_transcript_tree()
        return 1
    print(f"✅ 回答: {ans[:200]!r}")

    _dump_transcript_tree()
    print("\nOK: agy 経由の Gemini が OAuth サブスク枠で動作。"
          "SECOND_OPINION_GEMINI_MODE=auto（既定）で自動採用されます。")
    return 0


def _dump_transcript_tree() -> None:
    root = AGY_TRANSCRIPT_ROOT
    print(f"\n[transcript root] {root} exists={os.path.isdir(root)}")
    if not os.path.isdir(root):
        # 近縁パスを探索（実パスが違う場合の手掛かり）
        for cand in (
            os.path.expanduser("~/.gemini"),
            os.path.expanduser("~/.antigravity"),
            os.path.expanduser("~/.config/antigravity"),
        ):
            if os.path.isdir(cand):
                print(f"  候補ディレクトリあり: {cand}")
        return
    try:
        convs = sorted(
            (os.path.join(root, d) for d in os.listdir(root)),
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
            reverse=True,
        )[:2]
        for c in convs:
            jsonl = os.path.join(
                c, ".system_generated", "logs", "transcript.jsonl")
            print(f"  conv={os.path.basename(c)} transcript_exists="
                  f"{os.path.isfile(jsonl)} path={jsonl}")
    except OSError as exc:
        print(f"  走査失敗: {exc}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

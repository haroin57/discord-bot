"""agy (Antigravity CLI) runner — Google AI Pro/Ultra の OAuth サブスク枠で
Gemini を叩く（従量課金ゼロ）。SecondOpinion の Gemini 経路が使う。

背景（2026-07-02）: `generativelanguage.googleapis.com` API 直は AI Pro サブスクとは
別会計で prepay クレジット枯渇で落ちた。`agy -p` を subprocess で叩けば OS 資格ストアに
一度ログインした OAuth サブスク枠で動く。

肝となる罠（コミュニティ実測）:
  - `GEMINI_API_KEY` / `ANTIGRAVITY_API_KEY` が env にあると agy は OAuth をバイパスして
    従量課金 API に流れる（解決順は --api-key → 環境変数 → OAuth）。**必ず env から除去**
  - `agy -p` は print mode で Gemini 3.5 Flash (High) 固定（env/settings で上書き不可）
  - 非TTY で `agy -p` の stdout が消え exit 0 になる既知バグ → stdout 空なら transcript
    ファイル（JSONL）から最終回答を読む

失敗（未認証・timeout・transcript 欠損）は全て raise。呼び元（SecondOpinion の _one）が
fail-open で吸収し、API 直フォールバックへ回す。
"""

from __future__ import annotations

import json
import logging
import os
import shutil

from config import AGY_BIN, AGY_MODEL, AGY_TRANSCRIPT_ROOT, AGY_WORKDIR
from services.claude_api._cli_runner import AvailabilityCache, clean_env, run_cli

log = logging.getLogger(__name__)

# OAuth 枠を強制するため env から必ず除去するキー
_STRIP_ENV_KEYS = ("GEMINI_API_KEY", "ANTIGRAVITY_API_KEY", "GOOGLE_API_KEY")

# transcript JSONL の最終回答エントリの判定条件（コミュニティ実測）
_DONE_SOURCE = "MODEL"
_DONE_STATUS = "DONE"
_DONE_TYPE = "PLANNER_RESPONSE"

_avail = AvailabilityCache()


def _clean_env() -> dict:
    return clean_env(_STRIP_ENV_KEYS)


async def is_agy_available() -> bool:
    """agy バイナリが存在し認証済みかの軽量チェック（TTL キャッシュ）。

    `agy` が PATH に無ければ即 False。存在すれば `--version` 等で疎通確認する
    （認証状態の厳密判定は難しいので、実行時の失敗は ask_agy 側で捕捉する）。
    """

    async def _probe() -> bool:
        if not shutil.which(AGY_BIN):
            return False
        try:
            returncode, _out, _err = await run_cli(
                [AGY_BIN, "--version"],
                timeout_s=10, env=_clean_env(), cwd=None, label="agy",
            )
            return returncode == 0
        except Exception as exc:  # noqa: BLE001
            log.warning("agy availability check failed: %s", exc)
            return False

    return await _avail.get(_probe)


def _read_transcript_answer() -> "str | None":
    """最も新しい会話の transcript.jsonl から最終回答を読む。

    ~/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/transcript.jsonl
    実パスは Linux 実機で要検証（probe_agy.py で確定 → AGY_TRANSCRIPT_ROOT）。
    """
    root = AGY_TRANSCRIPT_ROOT
    if not os.path.isdir(root):
        return None
    # 最新の会話ディレクトリを mtime で選ぶ
    try:
        conv_dirs = [
            os.path.join(root, d) for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d))
        ]
    except OSError:
        return None
    if not conv_dirs:
        return None
    conv_dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for conv in conv_dirs[:3]:  # 最新3件まで見る（並行実行の取り違え保険）
        jsonl = os.path.join(
            conv, ".system_generated", "logs", "transcript.jsonl")
        answer = _parse_transcript_file(jsonl)
        if answer:
            return answer
    return None


def _parse_transcript_file(path: str) -> "str | None":
    if not os.path.isfile(path):
        return None
    last_answer: "str | None" = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    row.get("source") == _DONE_SOURCE
                    and row.get("status") == _DONE_STATUS
                    and row.get("type") == _DONE_TYPE
                ):
                    # 回答本文のキーは実測で確定する。候補を順に見る。
                    text = (
                        row.get("text")
                        or row.get("content")
                        or row.get("message")
                        or ""
                    )
                    if isinstance(text, str) and text.strip():
                        last_answer = text.strip()
    except OSError:
        return None
    return last_answer


async def ask_agy(prompt: str, *, timeout_s: int) -> str:
    """agy -p を OAuth サブスク枠で実行し、回答テキストを返す。

    stdout を一次、transcript.jsonl を二次で読む（非TTY stdout バグ対策）。
    未認証・timeout・回答取得不能は例外。
    """
    argv = [AGY_BIN, "-p", prompt]
    if AGY_MODEL:
        argv += ["--model", AGY_MODEL]
    # 専用の空 workdir（/tmp だと他プロセスのファイルで workspace 検出がハング）。
    try:
        os.makedirs(AGY_WORKDIR, exist_ok=True)
        cwd = AGY_WORKDIR
    except OSError:
        cwd = None
    returncode, stdout, stderr = await run_cli(
        argv, timeout_s=timeout_s, env=_clean_env(), cwd=cwd, label="agy",
    )
    stdout = stdout.strip()
    stderr = stderr.strip()

    if returncode != 0:
        # 認証切れ・quota 切れ等はここ（stderr にメッセージ）
        raise RuntimeError(
            f"agy exited {returncode}: {stderr[:300] or stdout[:300]}")

    if stdout:
        return stdout

    # stdout 空（非TTY バグ）→ transcript フォールバック
    answer = _read_transcript_answer()
    if answer:
        return answer
    raise RuntimeError(
        "agy produced no stdout and transcript answer not found "
        f"(stderr: {stderr[:200]})")


def _reset_cache_for_test() -> None:
    _avail.reset()

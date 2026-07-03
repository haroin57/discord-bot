"""codex (OpenAI Codex CLI) runner — ChatGPT Plus/Pro の OAuth サブスク枠で
GPT-5.5 を叩く（従量課金ゼロ）。SecondOpinion の GPT 経路が使う。

背景（2026-07-03）: GPT-5.5 は API に来ておらず ChatGPT サブスク枠限定。
`~/.codex/auth.json` が `auth_mode=chatgpt` でログイン済みなら、`codex exec` が
`backend-api/codex/responses`（サブスク credits）経由で GPT-5.5 を返す。agy(Gemini)
と全く同じ「サブスク枠優先、切れたら API」構図。

実測（2026-07-03, codex-cli 0.142.5）:
  - `gpt-5.5`（無印）= ChatGPT アカウントで使える最上位。`-codex`/`-pro`/`-codex-max`
    は "not supported when using Codex with a ChatGPT account" で 400
  - `-o/--output-last-message <FILE>` で最終メッセージがファイルに出る（stdout パースより堅い）
  - `--skip-git-repo-check -C <workdir>` で任意ディレクトリ実行

失敗（未認証・timeout・非対応モデル）は全て raise。呼び元（SecondOpinion の _one）が
fail-open で吸収し、API 直（gpt-5）フォールバックへ回す。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile

from config import (
    CODEX_BIN,
    CODEX_MODEL,
    CODEX_REASONING_EFFORT,
    CODEX_WORKDIR,
)
from services.claude_api._cli_runner import AvailabilityCache, clean_env, run_cli

log = logging.getLogger(__name__)

# OAuth サブスク枠を強制するため env から除去するキー（API キーがあると
# codex が ChatGPT 認証をバイパスして従量課金 API に流れ得る）
_STRIP_ENV_KEYS = ("OPENAI_API_KEY", "OPENAI_BASE_URL")

_avail = AvailabilityCache()


def _clean_env() -> dict:
    return clean_env(_STRIP_ENV_KEYS)


async def is_codex_available() -> bool:
    """codex バイナリが存在し ChatGPT 認証済みかの軽量チェック（TTL キャッシュ）。"""

    async def _probe() -> bool:
        if not shutil.which(CODEX_BIN):
            return False
        # ChatGPT ログイン済みか（auth.json の auth_mode）
        auth_path = os.path.expanduser("~/.codex/auth.json")
        try:
            import json
            with open(auth_path, encoding="utf-8") as f:
                return json.load(f).get("auth_mode") == "chatgpt"
        except (OSError, ValueError):
            return False

    return await _avail.get(_probe)


async def ask_codex(prompt: str, *, timeout_s: int) -> str:
    """codex exec を ChatGPT サブスク枠で実行し、最終メッセージを返す。

    -o で最終メッセージをファイルに出させ、それを読む（stdout パースより堅い）。
    未認証・timeout・非対応モデルは例外。
    """
    try:
        os.makedirs(CODEX_WORKDIR, exist_ok=True)
    except OSError:
        pass
    out_fd, out_path = tempfile.mkstemp(suffix=".txt", prefix="codex_")
    os.close(out_fd)
    argv = [
        CODEX_BIN, "exec",
        "--skip-git-repo-check",
        "-C", CODEX_WORKDIR,
        "-o", out_path,
    ]
    if CODEX_MODEL:
        argv += ["-m", CODEX_MODEL]
    if CODEX_REASONING_EFFORT:
        # SecondOpinion は即答性優先。config で -c model_reasoning_effort を渡す。
        argv += ["-c", f"model_reasoning_effort={CODEX_REASONING_EFFORT}"]
    argv.append(prompt)

    try:
        returncode, _stdout, stderr = await run_cli(
            argv, timeout_s=timeout_s, env=_clean_env(), cwd=CODEX_WORKDIR,
            label="codex",
        )
    except RuntimeError:
        _safe_unlink(out_path)
        raise

    try:
        answer = ""
        if os.path.isfile(out_path):
            with open(out_path, encoding="utf-8") as f:
                answer = f.read().strip()
    finally:
        _safe_unlink(out_path)

    if returncode != 0:
        # 認証切れ・quota 切れ・非対応モデルはここ（stderr にメッセージ）
        raise RuntimeError(
            f"codex exited {returncode}: {stderr[-300:].strip()}")
    if not answer:
        raise RuntimeError(
            f"codex produced no final message (stderr: {stderr[-200:].strip()})")
    return answer


def _safe_unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _reset_cache_for_test() -> None:
    _avail.reset()

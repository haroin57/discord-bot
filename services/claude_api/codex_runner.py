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

import asyncio
import logging
import os
import shutil
import tempfile
import time

from config import (
    CODEX_BIN,
    CODEX_MODEL,
    CODEX_REASONING_EFFORT,
    CODEX_WORKDIR,
)

log = logging.getLogger(__name__)

# OAuth サブスク枠を強制するため env から除去するキー（API キーがあると
# codex が ChatGPT 認証をバイパスして従量課金 API に流れ得る）
_STRIP_ENV_KEYS = ("OPENAI_API_KEY", "OPENAI_BASE_URL")

_avail_cache: "tuple[float, bool] | None" = None
_AVAIL_TTL_S = 300.0


def _clean_env() -> dict:
    env = dict(os.environ)
    for k in _STRIP_ENV_KEYS:
        env.pop(k, None)
    return env


async def is_codex_available() -> bool:
    """codex バイナリが存在し ChatGPT 認証済みかの軽量チェック（TTL キャッシュ）。"""
    global _avail_cache
    now = time.monotonic()
    if _avail_cache is not None and now - _avail_cache[0] < _AVAIL_TTL_S:
        return _avail_cache[1]
    ok = False
    if shutil.which(CODEX_BIN):
        # ChatGPT ログイン済みか（auth.json の auth_mode）
        auth_path = os.path.expanduser("~/.codex/auth.json")
        try:
            import json
            with open(auth_path, encoding="utf-8") as f:
                ok = json.load(f).get("auth_mode") == "chatgpt"
        except (OSError, ValueError):
            ok = False
    _avail_cache = (now, ok)
    return ok


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

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,  # MCP等でstdinがパイプだと子が入力待ちでハングする
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_clean_env(),          # サブスク枠強制（API キー除去）
        start_new_session=True,
        cwd=CODEX_WORKDIR,
    )
    try:
        _out_b, err_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.gather(proc.wait(), return_exceptions=True)
        _safe_unlink(out_path)
        raise RuntimeError(f"codex timed out after {timeout_s}s")

    stderr = (err_b or b"").decode("utf-8", "replace")
    try:
        answer = ""
        if os.path.isfile(out_path):
            with open(out_path, encoding="utf-8") as f:
                answer = f.read().strip()
    finally:
        _safe_unlink(out_path)

    if proc.returncode != 0:
        # 認証切れ・quota 切れ・非対応モデルはここ（stderr にメッセージ）
        raise RuntimeError(
            f"codex exited {proc.returncode}: {stderr[-300:].strip()}")
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
    global _avail_cache
    _avail_cache = None

"""ask_bridge — permission ask ハンドラのリクエストスコープ受け渡し＋承認判定。

bot.py（Discord 層）が call_with_tools を await する直前に set_ask_handler()
で「承認を待つ async ハンドラ」を設定する。ContextVar なので同一 asyncio
タスク（=1リクエストのループ）内でだけ見え、並行・後続リクエストには漏れ
ない。明示クリア不要（タスク終了で消える）。

ハンドラ署名: async (tool_name: str, detail: str, rule: str) -> bool

承認リプライの意図判定は正規表現でなく **LLM（Haiku・distillと同じ経路）** が
行う（judge_approval）。返り値: True=許可 / False=拒否 / None=判定不能
（呼び出し側は待機を継続する。拒否には倒さない）。
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

AskHandler = Callable[[str, str, str], Awaitable[bool]]

_ask_handler: ContextVar[AskHandler | None] = ContextVar(
    "permission_ask_handler", default=None,
)


def set_ask_handler(handler: AskHandler | None) -> None:
    _ask_handler.set(handler)


def get_ask_handler() -> AskHandler | None:
    return _ask_handler.get()


_JUDGE_TASK_PROMPT = (
    "あなたはツール実行の承認判定器です。管理者ユーザーの返信が、提示された"
    "ツール実行を「許可」しているか「拒否」しているかを判定し、"
    "approve / deny / unclear のいずれか **1語だけ** を出力してください。"
    "条件付き・曖昧・無関係・質問で判断できない場合は unclear。"
    "皮肉や口語（『いいけど気をつけて』=approve、『それはちょっと』=deny 等）"
    "も文意で判定すること。"
)

_JUDGE_TIMEOUT_S = 15


def _judge_model() -> str:
    try:
        from config import DELIB_DISTILL_MODEL

        return DELIB_DISTILL_MODEL
    except Exception:  # noqa: BLE001
        return "claude-haiku-4-5-20251001"


async def judge_approval(
    text: str, *, tool_name: str = "", rule: str = "", detail: str = "",
) -> bool | None:
    """リプライの承認意図を LLM で判定する。

    ✅/❌ 単体は自明なので API を呼ばず即断。LLM 失敗・timeout・unclear は
    None（=待機継続）。返信者は ADMIN_USER_IDS に限定されている前提
    （bot.py 側のチェックでゲート済み）なので注入リスクは限定的。
    """
    t = (text or "").strip()
    if not t:
        return None
    if t == "✅":
        return True
    if t == "❌":
        return False
    try:
        from services.mem0.client import get_anthropic_client
        from services.mem0.cli_compat import cli_messages_create, extract_text

        client = await get_anthropic_client()
        if client is None:
            log.warning("judge_approval: anthropic client unavailable")
            return None
        user_message = (
            f"ツール: {tool_name}\n"
            f"適用ルール: {rule}\n"
            f"実行しようとしている内容:\n{detail[:400]}\n\n"
            f"管理者の返信: {t[:400]}\n\n"
            "判定（approve / deny / unclear のいずれか1語）:"
        )
        resp = await asyncio.wait_for(
            cli_messages_create(
                client,
                model=_judge_model(),
                max_tokens=8,
                user_message=user_message,
                task_prompt=_JUDGE_TASK_PROMPT,
            ),
            timeout=_JUDGE_TIMEOUT_S,
        )
        verdict = extract_text(resp).strip().lower()
        if "approve" in verdict:
            return True
        if "deny" in verdict:
            return False
        return None
    except Exception as exc:  # noqa: BLE001 — 判定失敗は待機継続に倒す
        log.warning("judge_approval failed (non-fatal): %s", exc)
        return None

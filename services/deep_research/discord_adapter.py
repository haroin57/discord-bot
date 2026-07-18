"""Deep Research の Discord 表示と承認待機アダプター。"""

from __future__ import annotations

import asyncio

import discord

from services.claude_api.status import tool_oneline

from .run_state import ResearchRun

THREAD_NAME_MAX = 90


def short(text: str, n: int = 120) -> str:
    value = (text or "").strip().replace("\n", " ")
    return value if len(value) <= n else value[:n] + "…"


def thread_name(query: str) -> str:
    base = (query or "").strip().replace("\n", " ") or "deep research"
    return (f"🔬 {base}")[:THREAD_NAME_MAX] or "deep research"


async def safe_edit(msg: discord.Message | None, content: str) -> None:
    if msg is None:
        return
    try:
        await msg.edit(content=content[:1990])
    except discord.HTTPException:
        pass


async def safe_send(channel, content: str) -> None:
    try:
        await channel.send(content)
    except discord.HTTPException:
        pass


async def wait_for_reaction_or_reply(
    bot,
    uid,
    thread_id: int,
    target_msg,
    *,
    timeout_s: float,
    run: ResearchRun | None = None,
):
    """本人の reaction/message の先着を deadline 付きで待ち、全待機taskを回収する。"""

    def reaction_check(payload) -> bool:
        return (
            getattr(payload, "message_id", None) == getattr(target_msg, "id", None)
            and getattr(payload, "user_id", None) == uid
        )

    def message_check(message) -> bool:
        return (
            getattr(message.channel, "id", None) == thread_id
            and getattr(message.author, "id", None) == uid
            and not getattr(message.author, "bot", False)
        )

    reaction_task = asyncio.ensure_future(
        bot.wait_for("raw_reaction_add", check=reaction_check),
    )
    message_task = asyncio.ensure_future(bot.wait_for("message", check=message_check))
    if run is not None:
        run.track_task(reaction_task)
        run.track_task(message_task)
    done: set[asyncio.Task] = set()
    try:
        done, _ = await asyncio.wait(
            {reaction_task, message_task},
            timeout=max(0.0, timeout_s),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError
    finally:
        pending = [task for task in (reaction_task, message_task) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    if reaction_task in done and not reaction_task.cancelled():
        reaction_task.result()
        if message_task in done and not message_task.cancelled():
            message_task.exception()
        return "reaction", None
    return "message", message_task.result()


class _SearcherView:
    def __init__(self, idx: int, total: int, subq: str, msg) -> None:
        self.idx = idx
        self.total = total
        self.subq = subq
        self.msg = msg
        self.calls = 0
        self._t = 0.0
        self._label = f"[{idx}/{total}] {short(subq, 60)}"

    async def waiting(self) -> None:
        await safe_edit(self.msg, f"⏳ {self._label} — 待機中")

    async def searching(self) -> None:
        await safe_edit(self.msg, f"🔍 {self._label} — 探索中…")

    async def on_tool(self, name: str, tool_input: dict) -> None:
        self.calls += 1
        now = asyncio.get_running_loop().time()
        if now - self._t < 1.2:
            return
        self._t = now
        await safe_edit(
            self.msg, f"🔍 {self._label}\n-# {tool_oneline(name, tool_input)}"
        )

    async def finish(self, ok: bool) -> None:
        suffix = (
            f"✅ {self._label} — 検索 {self.calls} 回"
            if ok
            else f"❌ {self._label} — 失敗"
        )
        await safe_edit(self.msg, suffix)


class _AgentView:
    def __init__(self, label: str, msg, *, emoji: str = "🔍") -> None:
        self.label = label
        self.msg = msg
        self.emoji = emoji
        self.calls = 0
        self._t = 0.0

    async def waiting(self) -> None:
        await safe_edit(self.msg, f"⏳ {self.label} — 待機中")

    async def working(self, note: str = "作業中…") -> None:
        await safe_edit(self.msg, f"{self.emoji} {self.label} — {note}")

    async def on_tool(self, name: str, tool_input: dict) -> None:
        self.calls += 1
        now = asyncio.get_running_loop().time()
        if now - self._t < 1.2:
            return
        self._t = now
        await safe_edit(
            self.msg, f"{self.emoji} {self.label}\n-# {tool_oneline(name, tool_input)}"
        )

    async def finish(self, ok: bool, gist: str = "") -> None:
        if ok:
            tail = f" — {short(gist, 80)}" if gist else " — 完了"
            await safe_edit(self.msg, f"✅ {self.label}{tail}")
        else:
            await safe_edit(self.msg, f"❌ {self.label} — 失敗")


# 新しい責務名と既存 private class 名を同じ型に束ねる。
SearcherView = _SearcherView
AgentView = _AgentView

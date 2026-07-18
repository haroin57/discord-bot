"""Agent tool — メインパイプラインのサブエージェント spawn 入口（CLI 純正と足並み）.

公式 Claude Code CLI の Agent tool（handleSpawnInProcess / spawnInProcessTeammate）と
シグネチャを揃え、admin の本ルートでもモデルが ``Agent`` を呼んで fresh-context のサブ
エージェントに作業を委譲できるようにする。

設計:
- ツール名: ``Agent``（CLI バイナリ内の文字列とも一致）。
- 入力: ``{description, prompt, subagent_type?}``。subagent_type 既定 = "general-purpose"。
- 実体: ``services.deep_research.registry.run_subagent`` を流用する（fresh history・
  overloaded リトライ・MCP guard・bounded result・StallWatchdog 全部入り）。subagent は
  deep-research dispatcher が実行できる MCP tools だけを継承する。
- 並列: 1 親ターンで model が複数 ``Agent()`` を撃つと tool_loop の per-turn 並列実行に乗る
  ため、本モジュール側で ``asyncio.Semaphore(SUBAGENT_MAX_PARALLEL)`` で実走を律速する。
- セッション非汚染: 親の JSONL / sessions には一切触らない（``run_subagent`` の history=[] 仕様）。
- 再帰深さ: ``ctx.depth`` を子に +1 して渡し、上限到達で Agent ツールを子の advertised
  から除外することで暴走 fanout を防ぐ。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from config import (
    SUBAGENT_DEFAULT_TYPE,
    SUBAGENT_IDLE_TIMEOUT_S,
    SUBAGENT_MAX_PARALLEL,
    SUBAGENT_MAX_TURNS,
)
from services.claude_api.capabilities import mcp_tools_for_dispatcher

log = logging.getLogger(__name__)

# CLI の Agent tool description と揃える（バイナリ内文字列を直接参考に）。
_AGENT_TOOL_DESCRIPTION = (
    "Launch a new agent to handle complex, multi-step tasks. Each agent type has "
    "specific capabilities and tools available to it.\n\n"
    "Use this tool to delegate non-trivial work that is well-scoped enough to fit in a "
    "single prompt and that can be solved with a bounded textual result. The subagent "
    "runs in a fresh context (the parent's transcript is NOT shared) and returns only "
    "its final text as the tool_result.\n\n"
    "Specify a subagent_type parameter to select which agent type to use. If omitted, "
    "the general-purpose agent is used.\n\n"
    "When you launch multiple agents for independent work, send them in a single "
    "message with multiple tool uses so they run concurrently."
)


@dataclass(frozen=True)
class AgentSpec:
    """登録される subagent 型の宣言（CLI の AgentDefinition 相当）。"""

    name: str
    description: str  # 広告（schema の subagent_type enum description）
    system_prompt: str  # subagent の system_prompt（fresh context）
    # 親の advertised tools をそのまま流すなら True、絞るなら False（v1 はみな True）。
    inherit_tools: bool = True


# v1: general-purpose のみ。今後 .claude/agents/ から読む / 専門 agent を増やすときは
# ここに追加するだけ。
_AGENT_REGISTRY: dict[str, AgentSpec] = {
    "general-purpose": AgentSpec(
        name="general-purpose",
        description=(
            "General-purpose agent for researching complex questions, searching for "
            "code, and executing multi-step tasks. Inherits the parent's tool set and "
            "runs with a fresh context."
        ),
        system_prompt=(
            "You are a subagent dispatched from the parent agent. Your task is given "
            "in the user turn (prompt). Work autonomously using your available tools "
            "and return a concise, complete result as your final text — that text IS "
            "your tool_result to the parent (the parent will not see your tool calls "
            "or intermediate reasoning). Be thorough but bounded; do not ask follow-up "
            "questions. If a task is impossible or out of scope, return a brief "
            "explanation rather than partial work."
        ),
    ),
}


def list_agent_types() -> list[str]:
    return sorted(_AGENT_REGISTRY.keys())


def get_agent_spec(name: str) -> AgentSpec:
    """未知の subagent_type は general-purpose に fallback（CLI と同じ寛容な挙動）。"""
    return _AGENT_REGISTRY.get(name) or _AGENT_REGISTRY[SUBAGENT_DEFAULT_TYPE]


def agent_tool_schema() -> dict:
    """Anthropic Messages API に広告する Agent ツール定義。"""
    return {
        "name": "Agent",
        "description": _AGENT_TOOL_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the subagent to perform",
                },
                "subagent_type": {
                    "type": "string",
                    "enum": list_agent_types(),
                    "description": (
                        "The type of specialized agent to use for this task. "
                        "Defaults to 'general-purpose' if omitted."
                    ),
                },
            },
            "required": ["prompt", "description"],
        },
    }


@dataclass
class AgentToolCtx:
    """Agent tool の実行に必要な親側の依存（execute_tool 経路で注入）。"""

    api_client: Any
    pool: Any
    tools_for_api: list[dict]
    channel_id: int
    parent_model: str  # subagent は既定で親モデルを継承する（CLI と同じ）
    depth: int = 0
    # 並列に走る subagent を律速する semaphore（プロセス共有）。テストで差し替え可。
    sema: asyncio.Semaphore | None = None
    # subagent 内で MCP server_key 単位の lock を共有する（並列搾りで stdio 混線を防ぐ）。
    server_locks: dict = field(default_factory=dict)
    # 実行ログ・状態の任意 callback（UI 連携用。v1 は None）。
    status_callback: Callable | None = None
    # 親の StallWatchdog。subagent 実行中は親はストリームを受け取らないので、
    # 定期 heartbeat を入れないと idle_timeout で親が cancel される（haroin 2026-06-25）。
    parent_watchdog: Any = None


# subagent 実行中、親 watchdog にハートビートを入れる間隔（秒）。
# 親の `stall_threshold_s`（既定 30〜60s）より十分短くする必要がある。
_HEARTBEAT_INTERVAL_S = 5.0


async def _heartbeat_loop(watchdog) -> None:
    """親 StallWatchdog の idle カウンタを定期リセット。CancelledError で抜ける。"""
    try:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                watchdog.heartbeat()
            except Exception:  # noqa: BLE001 - never crash the heartbeat task
                log.debug("parent watchdog.heartbeat() raised", exc_info=True)
    except asyncio.CancelledError:
        return


# プロセス共有の subagent 並列度 semaphore（テストで差し替え可能なよう lazy init）。
_subagent_sema: asyncio.Semaphore | None = None


def _get_sema() -> asyncio.Semaphore:
    global _subagent_sema
    if _subagent_sema is None:
        _subagent_sema = asyncio.Semaphore(SUBAGENT_MAX_PARALLEL)
    return _subagent_sema


async def execute_agent_tool(args: dict, ctx: AgentToolCtx) -> str:
    """Agent ツール呼び出しを subagent 実行にディスパッチする。

    例外は握って tool_result 文字列に変換する（親ループを止めないため）。
    CancelledError のみ伝播（/abort 用）。
    """
    # 遅延 import: registry は config を読むので、起動コストと循環依存を避ける。
    from services.deep_research.registry import ResearchAgentDef, run_subagent
    from services.deep_research.types import ResearchState

    prompt = str(args.get("prompt") or "").strip()
    description = str(args.get("description") or "").strip()
    if not prompt:
        return "<tool_use_error>Agent: 'prompt' is required</tool_use_error>"
    subtype = (
        str(args.get("subagent_type") or SUBAGENT_DEFAULT_TYPE).strip()
        or SUBAGENT_DEFAULT_TYPE
    )
    spec = get_agent_spec(subtype)

    inherited_tools = (
        mcp_tools_for_dispatcher(ctx.tools_for_api) if spec.inherit_tools else []
    )

    sema = ctx.sema or _get_sema()
    defn = ResearchAgentDef(
        agent_type=f"subagent:{spec.name}",
        model=ctx.parent_model,  # 親モデルを継承（CLI Agent tool と同じ既定）
        system_prompt=spec.system_prompt,
        max_tool_turns=SUBAGENT_MAX_TURNS,
        idle_timeout_s=SUBAGENT_IDLE_TIMEOUT_S,
    )

    log.info(
        "Agent tool dispatch: subagent_type=%s depth=%d desc=%r prompt_len=%d",
        spec.name,
        ctx.depth,
        description[:60],
        len(prompt),
    )

    # subagent の usage 集計用に独立 state を作る（親の ResearchState とは分離）。
    state = ResearchState(query=prompt, clarified_query=prompt)

    # subagent 実行中は親の StallWatchdog にハートビートを入れ続ける。
    # 入れないと親が idle_timeout で cancel され「subagent が終わったあと統合する前に
    # 親がexit」が起きる（haroin 2026-06-25 実観測）。
    heartbeat_task: asyncio.Task | None = None
    if ctx.parent_watchdog is not None:
        heartbeat_task = asyncio.create_task(_heartbeat_loop(ctx.parent_watchdog))
    try:
        async with sema:
            result = await run_subagent(
                api_client=ctx.api_client,
                pool=ctx.pool,
                server_locks=ctx.server_locks,
                defn=defn,
                briefing=prompt,
                tools_for_api=inherited_tools,
                state=state,
                channel_id=ctx.channel_id,
                status_callback=ctx.status_callback,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - never crash the parent loop
        log.exception("Agent tool failed (subagent_type=%s)", spec.name)
        return f"<tool_use_error>Agent({spec.name}) failed: {type(exc).__name__}: {exc}</tool_use_error>"
    finally:
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # subagent 完了直後にもう一度親 watchdog をリセットしておく
        # （subagent の結果を親が「読む」までの間にも idle カウンタが進むため）。
        if ctx.parent_watchdog is not None:
            try:
                ctx.parent_watchdog.heartbeat()
            except Exception:  # noqa: BLE001
                pass

    if not result.ok:
        return f"<tool_use_error>Agent({spec.name}) returned no result: {result.error or 'empty'}</tool_use_error>"
    return result.summary_md or "(empty)"

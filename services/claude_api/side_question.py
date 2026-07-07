"""Side question ("/btw") — answer a quick aside in-context, WITHOUT persisting.

Mirrors claude-cli 2.1.170's ``/btw`` ("Ask a quick side question without
interrupting the main conversation"): it forks the current channel context,
runs a bounded agent turn, and writes **nothing** back to the JSONL transcript
/ replay window / search indexes. Non-persistence is *structural* — this module
never imports ``append_jsonl`` or ``set_session``, so there is no persistence
tail that a future edit could accidentally re-enable.

Reuses the same primitives as the main path (``services/claude_api/entry.py``):
``load_api_replay_channel`` for the read-only context, ``run_agent_loop`` for the
bounded loop (which itself performs no disk writes — see its module docstring),
``McpPool`` + ``select_mcp_servers`` for MCP, ``build_advertised_tools`` for the
tool schema, and ``make_can_use_tool`` for gating.

Safety: a side question leaves no audit trail, so destructive tools are denied
by default. It runs with a READ-ONLY tool allowlist (no Write / Edit / Bash /
Agent, no MCP mutation) enforced through ``make_can_use_tool(restricted=False,
allowed_tools=...)`` — the exact hook ``restricted.py`` scaffolded for "the
side-question feature". Pass ``allow_mutating_tools=True`` to lift the guard
(records-nothing destructive ops — use with care).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

from config import CLAUDE_EFFORT, CLAUDE_MODEL, CWD, MCP_SERVERS, SYSTEM_PROMPT
from services.mcp_selector import select_mcp_servers
from services.session import load_api_replay_channel

from services.claude_api.client import MODEL_BETAS
from services.claude_api.mcp_client import McpPool
from services.claude_api.restricted import make_can_use_tool
from services.claude_api.stall import StallWatchdog
from services.claude_api.tool_catalog import (
    build_advertised_tools,
    deferred_catalog_keys,
)
from services.claude_api.tool_loop import run_agent_loop
from services.claude_api.tools import ToolContext, execute_builtin_tool

log = logging.getLogger(__name__)

# Read-only builtins a side question may still use (no filesystem/shell writes,
# no subagent). Write / Edit / Bash / Agent are intentionally excluded.
_READONLY_BUILTINS = frozenset(
    {"Read", "Glob", "Grep", "TodoWrite", "Scratchpad", "SecondOpinion"}
)
# An MCP tool whose bare name contains any of these markers is treated as a
# mutation and dropped from the read-only allowlist (mem0 add/delete,
# cron create/delete, oracle image gen, etc.).
_MCP_MUTATION_MARKERS = (
    "add",
    "delete",
    "create",
    "update",
    "remove",
    "write",
    "image",
)


@dataclass
class SideQuestionResult:
    answer: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    prior_turns: int
    paused_reason: str | None = None


def _is_readonly_tool(name: str) -> bool:
    """True if ``name`` is a safe read-only tool for a non-persisted aside."""
    if name.startswith("mcp__"):
        bare = name.split("__", 2)[-1].lower()
        return not any(m in bare for m in _MCP_MUTATION_MARKERS)
    return name in _READONLY_BUILTINS


async def answer_side_question(
    channel_id: int,
    question: str,
    api_client,
    *,
    user_id: int | None = None,
    model: str | None = None,
    effort: str | None = None,
    author: str = "",
    max_turns: int = 2,
    allow_mutating_tools: bool = False,
) -> SideQuestionResult:
    """Answer ``question`` in the context of ``channel_id`` without persisting.

    Returns the assistant's text plus lightweight metrics. Never touches the
    JSONL transcript, the session store, or the search indexes.
    """
    t0 = time.monotonic()

    # 1. Read-only fork of the current channel context (compact-summary applied,
    #    tool blocks paired — same builder the main loop uses at entry.py:436).
    prior, _snapshot, _ts = await asyncio.to_thread(
        load_api_replay_channel, channel_id
    )
    # run_agent_loop mutates ``history`` in place and appends the prompt itself,
    # so hand it a copy and keep ``prior`` for the metric.
    history = list(prior)

    lead = "[Side question — 手短に。これはメイン会話に保存されない一時的な横質問です]"
    body = f"[発言者: {author}]\n{question}" if author else question
    prompt = f"{lead}\n{body}"

    async with AsyncExitStack() as stack:
        # 2. MCP pool: pick servers by the question text (same as the main path);
        #    everything spawnable is lazy, keyword hits are spawned eagerly so
        #    their tools show up in the advertised schema.
        try:
            chosen_cfg = await select_mcp_servers(question, user_id=user_id)
        except Exception as exc:  # MCP selection is best-effort; degrade to none
            log.warning("btw: select_mcp_servers failed: %s", exc)
            chosen_cfg = {}
        chosen_keys = set(chosen_cfg)
        spawnable = deferred_catalog_keys(user_id) | chosen_keys
        pool_available = {k: v for k, v in MCP_SERVERS.items() if k in spawnable}
        pool = McpPool(stack=stack, available_configs=pool_available)
        if chosen_cfg:
            await pool.spawn_eager(list(chosen_cfg))

        # 3. Advertise the full tool set, then filter to read-only unless the
        #    caller explicitly opted into mutations.
        advertised = build_advertised_tools(
            restricted=False,
            live_handles=pool.handles,
            always_keys=set(),
            hot_keys=chosen_keys,
            user_id=user_id,
            tool_search_enabled=False,
            agent_tool_schema=None,  # no subagent inside a side question
        )
        if allow_mutating_tools:
            tools = advertised
            can_use_tool = make_can_use_tool(restricted=False)
        else:
            tools = [
                t for t in advertised if _is_readonly_tool(t.get("name", ""))
            ]
            can_use_tool = make_can_use_tool(
                restricted=False,
                allowed_tools=[t["name"] for t in tools],
            )

        tool_ctx = ToolContext(cwd=CWD, restricted=False, channel_id=channel_id)

        async def _execute_tool(name: str, args: dict) -> str:
            # Defense in depth: refuse mutations even if one slipped past the
            # advertised-list filter (hallucinated tool name, etc.).
            if not allow_mutating_tools and not _is_readonly_tool(name):
                return (
                    "<tool_use_error>side question is read-only; "
                    f"{name} is not permitted</tool_use_error>"
                )
            if name.startswith("mcp__"):
                return await pool.call_tool(name, args)
            return await execute_builtin_tool(name, args, tool_ctx)

        # 4. Bounded, non-persisting agent loop. run_agent_loop mutates history
        #    in place and returns text/usage; it calls neither append_jsonl nor
        #    set_session (its module docstring guarantees the caller owns
        #    persistence, and we simply don't persist).
        async with StallWatchdog(channel_id) as watchdog:
            result = await run_agent_loop(
                prompt,
                channel_id,
                history=history,
                system_prompt=SYSTEM_PROMPT,
                tools_for_api=tools,
                can_use_tool=can_use_tool,
                execute_tool=_execute_tool,
                max_turns=max_turns,
                stall_watchdog=watchdog,
                api_client=api_client,
                model=model or CLAUDE_MODEL,
                extra_betas=MODEL_BETAS,
                budget_role="admin",
                output_config_override={"effort": effort or CLAUDE_EFFORT},
                active_mcp_keys=sorted(chosen_keys),
            )

    usage = result.usage_total or {}
    input_tokens = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    return SideQuestionResult(
        answer=result.text or "",
        input_tokens=input_tokens,
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        duration_ms=int((time.monotonic() - t0) * 1000),
        prior_turns=len(prior),
        paused_reason=result.paused_reason,
    )

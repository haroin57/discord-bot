"""Side question ("/btw") — answer a quick aside in-context, WITHOUT persisting.

Faithful port of claude-cli's ``/btw`` ("Ask a quick side question without
interrupting the main conversation"). Upstream (``src/utils/sideQuestion.ts``)
forks the current conversation context and runs a **single-turn, tool-less**
forked agent: it wraps the question in a ``<system-reminder>`` that tells the
model it is a separate lightweight instance with NO tools and exactly one shot
to answer from what it already knows, denies every tool call, and caps the run
at ``maxTurns: 1``. Nothing is written back to the transcript.

This module mirrors that contract on the Discord side:

* **No tools.** ``tools_for_api=[]`` — the request carries no ``tools`` field,
  so the model cannot emit a ``tool_use`` block. A deny-all ``can_use_tool``
  gate backs this up (defense in depth), exactly like upstream's
  ``canUseTool: async () => deny`` with the message "Side questions cannot use
  tools".
* **One turn.** ``max_turns=1`` — a single API call, no tool round-trips. Upstream
  is ``maxTurns: 1``.
* **Upstream system-reminder.** The question is wrapped in the same
  ``<system-reminder>`` framing so the model answers directly, in one response,
  without promising to "look it up" or "check".
* **Non-persistence is structural.** This module never imports ``append_jsonl``
  or ``set_session``, so there is no persistence tail a future edit could
  accidentally re-enable. ``load_api_replay_channel`` gives a read-only fork of
  the channel context (the compact-summary-applied equivalent of upstream's
  ``getMessagesAfterCompactBoundary``); ``run_agent_loop`` mutates only the
  in-memory ``history`` copy and writes nothing to disk (see its module
  docstring — persistence is the caller's job, and we simply don't).

Divergence from upstream, documented (ADR-0046): upstream keeps the main turn's
exact system/tool bytes (``cacheSafeParams`` + ``skipCacheWrite``) so the forked
request re-hits the prompt cache. That byte-capture machinery does not exist on
this side; advertising tools purely for cache-prefix parity would risk a
``turn_ceiling`` truncation under ``max_turns=1`` if the model tried to call one.
We therefore send no tools and rely on the already-cached system blocks
(``_build_cached_system``, 1h TTL). The observable behaviour — tool-less,
single-turn, answer-from-context — matches upstream; only the cache-hit
optimization is omitted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from config import CLAUDE_EFFORT, CLAUDE_MODEL, CWD, SYSTEM_PROMPT
from services.session import load_api_replay_channel

from services.claude_api.client import MODEL_BETAS
from services.claude_api.stall import StallWatchdog
from services.claude_api.tool_loop import run_agent_loop

log = logging.getLogger(__name__)

# The exact framing upstream (src/utils/sideQuestion.ts) wraps the question in.
# Kept verbatim (English) because it is a model-behaviour constraint layer, not
# persona copy — the answer still comes back in the bot's configured voice via
# SYSTEM_PROMPT. It steers the model to answer directly, tool-lessly, in one
# response, without promising follow-up actions.
_SIDE_QUESTION_REMINDER = (
    "<system-reminder>This is a side question from the user. You must answer "
    "this question directly in a single response.\n"
    "\n"
    "IMPORTANT CONTEXT:\n"
    "- You are a separate, lightweight agent spawned to answer this one "
    "question\n"
    "- The main agent is NOT interrupted - it continues working independently "
    "in the background\n"
    "- You share the conversation context but are a completely separate "
    "instance\n"
    "- Do NOT reference being interrupted or what you were \"previously doing\" "
    "- that framing is incorrect\n"
    "\n"
    "CRITICAL CONSTRAINTS:\n"
    "- You have NO tools available - you cannot read files, run commands, "
    "search, or take any actions\n"
    "- This is a one-off response - there will be no follow-up turns\n"
    "- You can ONLY provide information based on what you already know from the "
    "conversation context\n"
    "- NEVER say things like \"Let me try...\", \"I'll now...\", \"Let me "
    "check...\", or promise to take any action\n"
    "- If you don't know the answer, say so - do not offer to look it up or "
    "investigate\n"
    "\n"
    "Simply answer the question with the information you have."
    "</system-reminder>"
)


@dataclass
class SideQuestionResult:
    answer: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    prior_turns: int
    paused_reason: str | None = None


def _deny_all_tools(_name: str, _tool_input: dict) -> tuple[bool, str]:
    """can_use_tool gate that refuses everything (upstream parity)."""
    return False, "Side questions cannot use tools"


async def _reject_tool(name: str, _args: dict) -> str:
    """execute_tool stub. Never reached (tools=[]), but required by the loop."""
    return (
        "<tool_use_error>side questions cannot use tools; "
        f"{name} is not permitted</tool_use_error>"
    )


async def answer_side_question(
    channel_id: int,
    question: str,
    api_client,
    *,
    user_id: int | None = None,
    model: str | None = None,
    effort: str | None = None,
    author: str = "",
    max_turns: int = 1,
) -> SideQuestionResult:
    """Answer ``question`` in the context of ``channel_id`` without persisting.

    Runs a single tool-less turn (upstream ``/btw`` semantics) and returns the
    assistant's text plus lightweight metrics. Never touches the JSONL
    transcript, the session store, or the search indexes.
    """
    t0 = time.monotonic()

    # 1. Read-only fork of the current channel context (compact-summary applied,
    #    tool blocks paired — the equivalent of upstream's
    #    getMessagesAfterCompactBoundary). run_agent_loop mutates ``history`` in
    #    place and appends the prompt itself, so hand it a copy and keep
    #    ``prior`` for the metric.
    prior, _snapshot, _ts = await asyncio.to_thread(
        load_api_replay_channel, channel_id
    )
    history = list(prior)

    # 2. Wrap the question in the upstream side-question system-reminder. Like
    #    upstream, the reminder is injected into the user message (not the system
    #    prompt, which stays identical to the main path). The [発言者] label is a
    #    Discord-specific addition for multi-admin channels.
    body = f"[発言者: {author}]\n{question}" if author else question
    prompt = f"{_SIDE_QUESTION_REMINDER}\n\n{body}"

    # 3. Bounded, tool-less, non-persisting agent turn. No tools are advertised
    #    (the request carries no ``tools`` field, so the model cannot emit a
    #    tool_use block); the deny-all gate is belt-and-suspenders.
    async with StallWatchdog(channel_id) as watchdog:
        result = await run_agent_loop(
            prompt,
            channel_id,
            history=history,
            system_prompt=SYSTEM_PROMPT,
            tools_for_api=[],
            can_use_tool=_deny_all_tools,
            execute_tool=_reject_tool,
            max_turns=max_turns,
            stall_watchdog=watchdog,
            api_client=api_client,
            model=model or CLAUDE_MODEL,
            extra_betas=MODEL_BETAS,
            budget_role="admin",
            output_config_override={"effort": effort or CLAUDE_EFFORT},
            active_mcp_keys=[],
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

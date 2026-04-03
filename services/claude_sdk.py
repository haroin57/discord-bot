"""Claude Code SDK client — for tool-use tasks (Bash, Read, Write, etc.).

Matches free-code QueryEngine pattern: spawn CLI via SDK, stream events.
"""

import asyncio
import logging
import os

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    query,
)
from claude_code_sdk.types import ThinkingBlock, ToolUseBlock

from config import CLAUDE_TIMEOUT, CWD, SETTINGS_PATH, SYSTEM_PROMPT

log = logging.getLogger(__name__)

claude_semaphore = asyncio.Semaphore(1)  # 768M cgroup: max 1 CLI at a time


async def call_with_tools(
    prompt: str,
    channel_id: int,
    sessions: dict[int, str],
    status_callback=None,
    restricted: bool = False,
) -> tuple[str, str | None]:
    """Call Claude via SDK (CLI) for tool-use tasks. Returns (response_text, session_id)."""
    async with claude_semaphore:
        if restricted:
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                cwd=CWD,
                max_turns=3,
                settings=SETTINGS_PATH,
            )
        else:
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                cwd=CWD,
                max_turns=25,
                settings=SETTINGS_PATH,
            )

        if channel_id in sessions:
            options.resume = sessions[channel_id]

        response_text: str | None = None
        session_id: str | None = None

        try:
            async with asyncio.timeout(CLAUDE_TIMEOUT):
                async for msg in query(prompt=prompt, options=options):
                    if isinstance(msg, AssistantMessage) and status_callback:
                        for block in msg.content:
                            if isinstance(block, ThinkingBlock) and block.thinking:
                                summary = block.thinking[:80].replace("\n", " ")
                                await status_callback(f"💭 {summary}")
                            elif isinstance(block, ToolUseBlock):
                                await status_callback(f"🔧 {block.name}")
                    elif isinstance(msg, ResultMessage):
                        response_text = msg.result if hasattr(msg, "result") else str(msg)
                        session_id = msg.session_id if hasattr(msg, "session_id") else None
        except TimeoutError:
            return "⏰ Request timed out (10 min limit)", None
        except Exception as e:
            log.error("SDK error: %s", e, exc_info=True)
            return "❌ 内部エラーが発生しました。", None

        if response_text is None:
            return "(No response from Claude)", None

        return response_text.strip() or "(No response from Claude)", session_id

"""Claude Code SDK client — for tool-use tasks (Bash, Read, Write, etc.).

Matches free-code QueryEngine pattern: spawn CLI via SDK, stream events.
Dual-model routing: Haiku for chat, Sonnet for tool-use.
"""

import asyncio
import io
import logging
import os
import sys

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    query,
)
from claude_code_sdk.types import ThinkingBlock, ToolUseBlock

from config import CLAUDE_TIMEOUT, CWD, MCP_SERVERS, SETTINGS_PATH, SYSTEM_PROMPT, needs_tools

log = logging.getLogger(__name__)

claude_semaphore = asyncio.Semaphore(1)  # 768M cgroup: max 1 CLI at a time

# Default model for all requests
CHAT_MODEL = "claude-opus-4-6"
# Full model for tool-use tasks
TOOL_MODEL = "claude-opus-4-6"



async def call_with_tools(
    prompt: str,
    channel_id: int,
    sessions: dict[int, str],
    status_callback=None,
    restricted: bool = False,
    raw_user_text: str = "",
) -> tuple[str, str | None]:
    """Call Claude via SDK (CLI). Routes to Haiku (fast) or Sonnet (tools) based on content."""
    async with claude_semaphore:
        has_images = "Use the Read tool to view this image" in prompt
        use_tools = needs_tools(raw_user_text or prompt) or has_images

        if restricted:
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                cwd=CWD,
                max_turns=1,
                model=CHAT_MODEL,
                mcp_servers=MCP_SERVERS,
                disallowed_tools=[
                    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
                    "WebFetch", "WebSearch", "Agent",
                ],
                settings=SETTINGS_PATH,
                debug_stderr=sys.stderr,
            )
        elif use_tools:
            # Full model with tools
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                cwd=CWD,
                max_turns=25,
                model=TOOL_MODEL,
                mcp_servers=MCP_SERVERS,
                settings=SETTINGS_PATH,
                debug_stderr=sys.stderr,
            )
        else:
            # Chat mode, minimal turns, no heavy tools
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                cwd=CWD,
                max_turns=1,
                model=CHAT_MODEL,
                mcp_servers=MCP_SERVERS,
                disallowed_tools=["Bash", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"],
                settings=SETTINGS_PATH,
                debug_stderr=sys.stderr,
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
            # Clear broken session to prevent resume loop
            if channel_id in sessions:
                del sessions[channel_id]
                log.info("Cleared broken session for channel %d", channel_id)
            return "❌ 内部エラーが発生しました。再試行してください。", None

        if response_text is None:
            return "(No response from Claude)", None

        log.info("Response via %s (%s mode)", CHAT_MODEL if not use_tools else TOOL_MODEL, "tool" if use_tools else "chat")
        return response_text.strip() or "(No response from Claude)", session_id

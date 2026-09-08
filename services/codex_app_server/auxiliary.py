"""Bounded fresh-context helper tasks using the bot's ChatGPT OAuth account.

Auxiliary tasks never inherit the dangerous admin tool surface. Native host
tools, personal MCP servers, plugins, skills, hooks, and browser access are
disabled. Only explicitly supplied dynamic tools are callable through the
host allowlist. No Anthropic or usage-billed OpenAI API client is created.
"""
from __future__ import annotations

import asyncio
import os
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from services.codex_app_server.session import CodexSession

_SEMAPHORES: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_DISABLED_FEATURES = (
    'shell_tool', 'view_image', 'browser_use', 'browser_use_external',
    'computer_use', 'multi_agent', 'multi_agent_v2', 'apps', 'plugins',
    'remote_plugin', 'code_mode', 'code_mode_host', 'skill_search',
    'workspace_dependencies', 'hooks', 'image_generation', 'memories',
    'sleep_tool', 'tool_suggest',
)


def enabled() -> bool:
    return os.environ.get('BOT_AUX_LLM_PROVIDER', '').lower() == 'codex'


def _semaphore() -> asyncio.Semaphore:
    # Mem0 also invokes this adapter from a worker-owned asyncio.run loop.
    # Never bind a process-global asyncio primitive to another event loop.
    loop = asyncio.get_running_loop()
    semaphore = _SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(2)
        _SEMAPHORES[loop] = semaphore
    return semaphore


def normalized_usage(result) -> dict[str, int]:
    """Map the native per-turn counters to messages-style usage fields."""
    usage = result.token_usage_last or {}
    def count(*keys):
        return max(0, next((int(usage[key]) for key in keys
                           if isinstance(usage.get(key), (int, float))), 0))
    cached = count('cachedInputTokens', 'cached_input_tokens')
    return {'input_tokens': max(0, count('inputTokens', 'input_tokens') - cached),
            'output_tokens': count('outputTokens', 'output_tokens'),
            'cache_read_input_tokens': cached, 'cache_creation_input_tokens': 0}


def isolated_args() -> list[str]:
    from services.codex_app_server.manager import _global_codex_config
    args = ['-c', 'mcp_servers={}', '-c', 'web_search="disabled"',
            '-c', 'project_doc_max_bytes=0', '-c', 'features.skip_host_skill_discovery=true',
            '-c', 'apps._default.enabled=false']
    for feature in _DISABLED_FEATURES:
        args.extend(['-c', f'features.{feature}=false'])
    for name in (_global_codex_config().get('mcp_servers') or {}):
        import re
        if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(name)):
            args.extend(['-c', f'mcp_servers.{name}.enabled=false'])
    return args


async def run_task(*, prompt: str, instructions: str, tools: list[dict] | None = None,
                   execute_tool=None, effort: str = 'low', timeout_s: float = 120,
                   max_tool_calls: int = 20, text_stream_callback=None,
                   thinking_stream_callback=None, thinking_finalize_callback=None):
    from config import CODEX_BIN, CODEX_APP_SERVER_MODEL, CODEX_APP_SERVER_PATH, STATE_DIR
    cwd = Path(STATE_DIR) / 'codex_auxiliary'
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    dynamic = [{'name': tool['name'], 'description': tool.get('description', ''),
                'inputSchema': tool.get('input_schema', {'type': 'object', 'properties': {}})}
               for tool in (tools or [])]
    args = isolated_args()
    if dynamic:
        # GPT-6 dispatches dynamic tools through its isolated code-mode host.
        # Native filesystem/network tools remain disabled above.
        args.extend(['-c', 'features.code_mode_host=true'])
    session = CodexSession(cwd=str(cwd), codex_bin=CODEX_BIN,
                           extra_args=args, sandbox='read-only', approval_policy='never',
                           child_env={'PATH': CODEX_APP_SERVER_PATH}, dynamic_tools=dynamic,
                           tool_executor=execute_tool, ephemeral=True, max_tool_calls=max_tool_calls,
                           allow_host_approvals=False, allow_local_images=False,
                           turn_timeout_s=timeout_s, post_tool_timeout_s=min(180, timeout_s))
    async with _semaphore():
        try:
            async with asyncio.timeout(timeout_s):
                return await session.run_turn(
                    prompt, model=CODEX_APP_SERVER_MODEL, effort=effort,
                    developer_instructions=instructions,
                    text_stream_callback=text_stream_callback,
                    thinking_stream_callback=thinking_stream_callback,
                    thinking_finalize_callback=thinking_finalize_callback,
                )
        finally:
            await session.close()


class CodexTextClient:
    """Small compatibility object for existing messages-style helper calls."""
    def __init__(self):
        self.messages = self

    async def complete(self, *, system: str, prompt: str, max_tokens: int = 2000):
        result = await run_task(prompt=prompt, instructions=system +
                               f'\nReturn only the requested result, within approximately {max_tokens} output tokens. Do not use tools.')
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=result.final_text)],
                               stop_reason='end_turn', model=result.model,
                               usage=SimpleNamespace(**normalized_usage(result)))

    async def create(self, **kwargs: Any):
        system = kwargs.get('system', '')
        if isinstance(system, list):
            system = '\n'.join(item.get('text', '') for item in system if isinstance(item, dict))
        messages = kwargs.get('messages', [])
        prompt = '\n'.join(str(item.get('content', '')) for item in messages)
        return await self.complete(system=str(system), prompt=prompt, max_tokens=kwargs.get('max_tokens', 2000))

    async def close(self):
        return None

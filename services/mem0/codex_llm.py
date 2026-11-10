"""Mem0's synchronous extraction adapter backed by ChatGPT-authenticated Codex.

Mem0 invokes this from its worker thread. No API key, Anthropic client, host
tool, or shared interactive thread is involved. Invalid JSON fails closed.
"""
from __future__ import annotations

import asyncio
import json

from mem0.llms.base import LLMBase
from mem0.llms.configs import LlmConfig
from pydantic import field_validator


class CodexLlmConfig(LlmConfig):
    @field_validator('config')
    @classmethod
    def validate_config(cls, value, info):
        if info.data.get('provider') != 'codex':
            raise ValueError('This configuration only supports Codex')
        return value

    def __getitem__(self, key):
        # Both bot and Mem0-alpha inspect the config as a mapping for logging.
        if key not in {'provider', 'config'}:
            raise KeyError(key)
        return getattr(self, key)


def configuration(model: str) -> CodexLlmConfig:
    from mem0.utils.factory import LlmFactory
    LlmFactory.register_provider('codex', 'services.mem0.codex_llm.CodexLLM')
    # MemoryConfig accepts this validated LlmConfig subclass. This avoids
    # replacing or weakening Mem0's global, built-in provider validator.
    return CodexLlmConfig(provider='codex', config={'model': model, 'max_tokens': 2000})


class CodexLLM(LLMBase):
    def generate_response(self, messages, tools=None, tool_choice='auto', **kwargs):
        if tools:
            raise ValueError('Mem0 Codex extraction does not execute tools')
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError('Synchronous Mem0 extraction must run in a worker thread')
        from services.codex_app_server import auxiliary
        system = '\n'.join(str(m.get('content', '')) for m in messages if m.get('role') == 'system')
        prompt = json.dumps([m for m in messages if m.get('role') != 'system'], ensure_ascii=False)
        json_mode = bool(kwargs.get('response_format'))
        if json_mode:
            system += '\nReturn exactly the requested JSON object, without Markdown or commentary.'
        result = asyncio.run(auxiliary.run_task(prompt=prompt, instructions=system, timeout_s=120))
        text = result.final_text.strip()
        if json_mode:
            if text.startswith('```') and text.endswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError('Mem0 extraction must return a JSON object')
            return json.dumps(parsed, ensure_ascii=False)
        return text

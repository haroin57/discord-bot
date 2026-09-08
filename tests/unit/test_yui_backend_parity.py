from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def test_codex_mem0_config_validates_without_changing_builtin_providers(monkeypatch):
    from services.mem0 import client
    from mem0.configs.base import MemoryConfig
    from mem0.llms.configs import LlmConfig
    from mem0.utils.factory import LlmFactory
    monkeypatch.setenv('BOT_AUX_LLM_PROVIDER', 'codex')
    monkeypatch.setattr(client, 'oauth_init', Mock(side_effect=AssertionError('Claude must not initialize')))
    cfg = client._build_config()
    validated = MemoryConfig(**cfg)
    assert validated.llm.provider == 'codex'
    assert cfg['llm']['config']['model'] == validated.llm.config['model']
    adapter = LlmFactory.create(validated.llm.provider, validated.llm.config)
    assert type(adapter).__name__ == 'CodexLLM'
    with pytest.raises(ValueError):
        LlmConfig(provider='unknown', config={})


def test_mem0_sync_codex_json(monkeypatch):
    from services.mem0.codex_llm import CodexLLM
    from services.codex_app_server import auxiliary
    run = AsyncMock(return_value=SimpleNamespace(final_text='```json\n{"facts":["test"]}\n```'))
    monkeypatch.setattr(auxiliary, 'run_task', run)
    adapter = CodexLLM({'model': 'gpt-6-astra'})
    result = adapter.generate_response([{'role':'user','content':'example'}], response_format={'type':'json_object'})
    assert json.loads(result) == {'facts':['test']}
    assert 'tools' not in run.call_args.kwargs


def test_mem0_sync_codex_rejects_invalid_json_and_tools(monkeypatch):
    from services.mem0.codex_llm import CodexLLM
    from services.codex_app_server import auxiliary
    run = AsyncMock(return_value=SimpleNamespace(final_text='unverified text'))
    monkeypatch.setattr(auxiliary, 'run_task', run)
    adapter = CodexLLM({'model': 'gpt-6-astra'})
    with pytest.raises(ValueError):
        adapter.generate_response([], response_format={'type':'json_object'})
    with pytest.raises(ValueError, match='tools'):
        adapter.generate_response([], tools=[{'name':'shell'}])


@pytest.mark.asyncio
async def test_mem0_codex_skips_claude_refresh(monkeypatch):
    from services.mem0 import client
    monkeypatch.setenv('BOT_AUX_LLM_PROVIDER', 'codex')
    refresh = AsyncMock(side_effect=AssertionError('Claude must not refresh'))
    monkeypatch.setattr(client, 'ensure_valid_token', refresh)
    await client._refresh_token_if_available()
    assert type(await client.get_anthropic_client()).__name__ == 'CodexTextClient'
    refresh.assert_not_called()


def test_bridge_config_rejects_public_and_symlink(tmp_path):
    from mcp_codex_bridge import load_config
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'version':1,'servers':{}}))
    path.chmod(0o600)
    assert load_config(path)['version'] == 1
    path.chmod(0o644)
    with pytest.raises(ValueError, match='private'):
        load_config(path)
    path.chmod(0o600)
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_config(link)


def test_bridge_personal_authority(monkeypatch):
    import config
    from services.codex_app_server import mcp_bridge
    monkeypatch.setattr(config, 'ADMIN_USER_IDS', {1,2})
    monkeypatch.setattr(config, 'KANAE_ALARM_ALLOWED_USER_IDS', {1})
    monkeypatch.setattr(config, 'NATURE_REMO_ALLOWED_USER_IDS', {1})
    monkeypatch.setattr(config, 'AGENTIC_INBOX_ALLOWED_USER_IDS', {1})
    for name in ['kanae-alarm','nature-remo','agentic-inbox']:
        assert mcp_bridge.server_allowed(name,1)
        assert not mcp_bridge.server_allowed(name,2)
        assert not mcp_bridge.server_allowed(name,None)
    assert mcp_bridge.server_allowed('brave',2)
    assert not mcp_bridge.server_allowed('brave',3)


def test_bridge_args_keep_credentials_off_argv(monkeypatch, tmp_path):
    import config
    from services.codex_app_server import mcp_bridge
    monkeypatch.setattr(config, 'STATE_DIR', str(tmp_path))
    monkeypatch.setattr(mcp_bridge, 'authorized_configs', lambda uid: {'brave':{'command':'tool','env':{'API_KEY':'test-secret'}}})
    args = mcp_bridge.bridge_args(1)
    assert 'test-secret' not in repr(args)
    path = tmp_path / 'codex_mcp/1/config.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())['servers']['brave']['env']['API_KEY'] == 'test-secret'
    path.write_text('corrupt')
    mcp_bridge.bridge_args(1)
    assert json.loads(path.read_text())['user_id'] == 1


@pytest.mark.asyncio
async def test_bridge_actor_timeout_does_not_block_next_call(monkeypatch):
    from mcp_codex_bridge import run_actor
    from services.claude_api import mcp_client
    owners = []
    async def spawn(name):
        owners.append(asyncio.current_task())
        return SimpleNamespace(tools=[])
    async def call(name, args):
        owners.append(asyncio.current_task())
        if args.get('hang'):
            await asyncio.Event().wait()
        return 'ok'
    pool = SimpleNamespace(ensure_spawned=spawn,call_tool=call)
    monkeypatch.setattr(mcp_client,'McpPool',lambda **kwargs:pool)
    queue = asyncio.Queue()
    task = asyncio.create_task(run_actor(queue,{'brave':{}},timeout_s=0.02))
    loop = asyncio.get_running_loop()
    first, second, skipped = loop.create_future(), loop.create_future(), loop.create_future()
    skipped.cancel()
    try:
        for future,args in [(skipped,{}),(first,{'hang':True}),(second,{})]:
            await queue.put(('call','mcp__brave__search',args,future))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(first,1)
        assert await asyncio.wait_for(second,1) == 'ok'
        assert owners == [task]*4
    finally:
        task.cancel()
        await task


@pytest.mark.asyncio
async def test_research_codex_uses_allowlist_sources_and_usage(monkeypatch):
    from services.deep_research import registry
    from services.codex_app_server import auxiliary
    monkeypatch.setenv('BOT_AUX_LLM_PROVIDER','codex')
    state = SimpleNamespace(add_usage=Mock())
    source_parser = Mock()
    monkeypatch.setattr(registry, 'parse_sources',source_parser)
    pool = SimpleNamespace(call_tool=AsyncMock(return_value='source https://example.com'))
    async def run(**kwargs):
        assert 'Bash' not in [x['name'] for x in kwargs['tools']]
        await kwargs['execute_tool']('mcp__brave__search',{'query':'test'})
        denied = await kwargs['execute_tool']('Bash',{})
        assert 'Deep Research' in denied
        return SimpleNamespace(final_text='summary',token_usage_last={'inputTokens':10,'cachedInputTokens':3,'outputTokens':5})
    monkeypatch.setattr(auxiliary, 'run_task',run)
    result = await registry.run_subagent(api_client=None,pool=pool,server_locks={},
        defn=registry.ResearchAgentDef('researcher','claude-old','system',2),briefing='test',
        tools_for_api=[{'name':'mcp__brave__search','input_schema':{'type':'object'}}],state=state,channel_id=1)
    assert result.ok and result.summary_md == 'summary'
    assert pool.call_tool.await_count == 1
    source_parser.assert_called_once()
    usage = state.add_usage.call_args.args[0]
    assert usage['input_tokens'] + usage['cache_read_input_tokens'] == 10


@pytest.mark.asyncio
async def test_research_codex_cancellation_propagates(monkeypatch):
    from services.deep_research import registry
    from services.codex_app_server import auxiliary
    monkeypatch.setenv('BOT_AUX_LLM_PROVIDER','codex')
    monkeypatch.setattr(auxiliary,'run_task',AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await registry.run_subagent(api_client=None,pool=None,server_locks={},
            defn=registry.ResearchAgentDef('researcher','claude-old','system',2),briefing='test',
            tools_for_api=[],state=SimpleNamespace(add_usage=Mock()),channel_id=1)


@pytest.mark.asyncio
async def test_research_clarifier_uses_codex(monkeypatch):
    from services.deep_research import orchestrator
    from services.codex_app_server import auxiliary
    monkeypatch.setenv('BOT_AUX_LLM_PROVIDER','codex')
    run = AsyncMock(return_value=SimpleNamespace(final_text='scope?',token_usage_last=None))
    monkeypatch.setattr(auxiliary,'run_task',run)
    monkeypatch.setattr(orchestrator,'run_agent_loop',AsyncMock(side_effect=AssertionError('Claude forbidden')))
    state = SimpleNamespace(user_memories='',add_usage=Mock())
    await orchestrator._clarify(SimpleNamespace(id=1),SimpleNamespace(edit=AsyncMock()),None,'test',state,None,None)
    run.assert_awaited_once()

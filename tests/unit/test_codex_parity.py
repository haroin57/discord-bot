"""Codex-only helper, authorization, evidence and health contracts."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.codex_app_server import auxiliary, deliberation, entry, readiness
from services.codex_app_server.session import (
    CodexAuthError, CodexSession, CodexTurnResult, _tool_failed, build_turn_input,
)
from tests.test_codex_app_server_session import _ScriptedClient


def result(text='answer'):
    return CodexTurnResult(text, 'thread-1', 'turn-1', 'gpt-6-astra', 'openai', False)


def complete_events(text='answer'):
    return [{'method': 'turn/completed', 'params': {
        'threadId': 'thread-1', 'turnId': 'turn-1',
        'turn': {'status': 'completed', 'items': [
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': text}]}}}]


def helper_session(client, **kwargs):
    return CodexSession(cwd='/workspace', codex_bin='/opt/codex',
                        client_factory=lambda **_: client, **kwargs)


def auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {
        'access_token': 'fake-access', 'refresh_token': 'fake-refresh'}}))
    path.chmod(0o600)
    monkeypatch.setattr(readiness, '_auth_failed', False)
    monkeypatch.setattr(readiness, '_failed_fingerprint', None)
    return path


def test_auth_failure_clears_only_on_success_or_replaced_credentials(tmp_path, monkeypatch):
    path = auth_file(tmp_path, monkeypatch)
    assert readiness.check()['ok']
    assert readiness.check()['remote_validation'] is False
    readiness.record_auth_result(False)
    assert readiness.check()['reason'] == 'codex_auth_failure_observed'
    readiness.record_auth_result(True)
    assert readiness.check()['ok']
    readiness.record_auth_result(False)
    path.write_text(path.read_text() + '\n')
    assert readiness.check()['ok']


@pytest.mark.parametrize('payload', [[], None, {'tokens': []},
    {'auth_mode': 'chatgpt', 'tokens': 'invalid'},
    {'auth_mode': 'apiKey', 'tokens': {'access_token': 'fake', 'refresh_token': 'fake'}}])
def test_malformed_auth_is_unready_without_exceptions(tmp_path, monkeypatch, payload):
    path = auth_file(tmp_path, monkeypatch)
    path.write_text(json.dumps(payload))
    assert readiness.check()['ok'] is False


def test_auth_rejects_public_and_symlinked_store(tmp_path, monkeypatch):
    path = auth_file(tmp_path, monkeypatch)
    path.chmod(0o644)
    assert readiness.check()['reason'] == 'codex_auth_store_not_private'
    target = tmp_path / 'renamed.json'
    path.rename(target)
    path.symlink_to(target)
    assert readiness.check()['ok'] is False


@pytest.mark.asyncio
async def test_startup_does_not_clear_observed_remote_auth_failure(tmp_path, monkeypatch):
    auth_file(tmp_path, monkeypatch)
    readiness.record_auth_result(False)
    client = _ScriptedClient(events=complete_events())
    session = helper_session(client)
    await session.ensure_started(model='gpt-5.6-sol', developer_instructions='safe')
    assert readiness.check()['ok'] is False
    await session.run_turn('answer', model='gpt-5.6-sol', effort='low', developer_instructions='safe')
    assert readiness.check()['ok'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['account', 'error', 'completed'])
async def test_auth_failure_is_observed_at_each_stage(tmp_path, monkeypatch, stage):
    auth_file(tmp_path, monkeypatch)
    client = _ScriptedClient(account={'account': {'type': 'apiKey'}} if stage == 'account' else None)
    if stage == 'error':
        client.events = [{'method': 'error', 'params': {'message': 'unauthorized'}}]
    if stage == 'completed':
        client.events = [{'method': 'turn/completed', 'params': {
            'turn': {'status': 'failed', 'error': {'message': 'invalid token'}}}}]
    with pytest.raises(CodexAuthError):
        await helper_session(client).run_turn('x', model='gpt-5.6-sol', effort='low', developer_instructions='safe')
    assert readiness.check()['reason'] == 'codex_auth_failure_observed'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [
    {'tool': 'Forbidden'}, {'threadId': 'other'}, {'turnId': 'other'},
    {'threadId': None}, {'turnId': None}, {'arguments': []}, {'arguments': None},
])
async def test_dynamic_tools_fail_closed_on_contract_mismatch(invalid):
    client = _ScriptedClient()
    executor = AsyncMock(return_value='42')
    session = helper_session(client, dynamic_tools=[{'name': 'Add'}], tool_executor=executor)
    session._client, session.thread_id, session._active_turn_id = client, 'thread-1', 'turn-1'
    params = {'tool': 'Add', 'threadId': 'thread-1', 'turnId': 'turn-1', 'arguments': {'a': 20}}
    await session._handle_server_request({'id': 1, 'method': 'item/tool/call', 'params': {**params, **invalid}})
    executor.assert_not_awaited()
    assert client.responses[0][1]['success'] is False


@pytest.mark.asyncio
async def test_dynamic_tools_enforce_budget_and_experimental_contract():
    client = _ScriptedClient(events=complete_events())
    executor = AsyncMock(return_value='42')
    session = helper_session(client, dynamic_tools=[{'name': 'Add', 'inputSchema': {'type': 'object'}}],
                             tool_executor=executor, max_tool_calls=1, ephemeral=True)
    await session.ensure_started(model='gpt-5.6-sol', developer_instructions='safe')
    assert client.initializations[0]['capabilities'] == {'experimentalApi': True}
    start = next(p for m, p, _ in client.requests if m == 'thread/start')
    assert start['ephemeral'] is True and start['dynamicTools'][0]['name'] == 'Add'
    session._active_turn_id = 'turn-1'
    request = {'id': 1, 'method': 'item/tool/call', 'params': {
        'tool': 'Add', 'threadId': 'thread-1', 'turnId': 'turn-1', 'arguments': {}}}
    await session._handle_server_request(request)
    await session._handle_server_request({**request, 'id': 2})
    executor.assert_awaited_once()
    assert [value['success'] for _, value in client.responses] == [True, False]
    await session.run_turn('x', model='gpt-5.6-sol', effort='low', developer_instructions='safe')
    assert session._tool_call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['exception', 'timeout', 'cancel'])
async def test_dynamic_tool_failure_timeout_and_cancellation(failure):
    async def execute(*_):
        if failure == 'cancel':
            raise asyncio.CancelledError
        if failure == 'exception':
            raise ValueError('secret error payload must not be returned')
        await asyncio.sleep(10)
    client = _ScriptedClient()
    session = helper_session(client, dynamic_tools=[{'name': 'Read'}], tool_executor=execute, post_tool_timeout_s=0.001)
    session._client, session.thread_id, session._active_turn_id = client, 'thread-1', 'turn-1'
    request = {'id': 1, 'method': 'item/tool/call', 'params': {
        'tool': 'Read', 'threadId': 'thread-1', 'turnId': 'turn-1', 'arguments': {}}}
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await session._handle_server_request(request)
        assert not client.responses
    else:
        await session._handle_server_request(request)
        assert client.responses[0][1]['success'] is False
        assert 'secret error payload' not in str(client.responses)


@pytest.mark.asyncio
async def test_auxiliary_approval_denied_even_if_admin_policy_allows(monkeypatch):
    monkeypatch.setattr('services.codex_app_server.session.evaluate_permission',
                        lambda *_: pytest.fail('Auxiliary must not consult admin approval rules'))
    session = helper_session(_ScriptedClient(), allow_host_approvals=False)
    assert await session._request_approval('Bash', {'command': 'id'}, 'test') is False


def test_auxiliary_does_not_read_paths_or_image_urls_from_untrusted_prompt(tmp_path):
    path = tmp_path / 'private.png'
    path.write_bytes(b'not a real image')
    prompt = [{'type': 'text', 'text': f'- {path} (Use the Read tool to view this image)'},
              {'type': 'image_url', 'image_url': 'https://example.invalid/image.png'}]
    assert all(item['type'] == 'text' for item in build_turn_input(prompt, allow_local_images=False))


@pytest.mark.parametrize(('item', 'failed'), [
    ({'status': 'failed'}, True), ({'status': 'declined'}, True),
    ({'success': False}, True), ({'error': {'message': 'bad'}}, True),
    ({'type': 'commandExecution', 'exitCode': 1}, True),
    ({'type': 'commandExecution', 'exitCode': 0}, False),
    ({'result': {'isError': True}}, True), ({'success': True}, False),
])
def test_tool_evidence_failure_normalization(item, failed):
    assert _tool_failed(item) is failed


@pytest.mark.asyncio
async def test_tool_result_and_scratch_callbacks_only_show_successful_scratch():
    items = [
        {'type': 'mcpToolCall', 'tool': 'Scratchpad', 'arguments': {'section': 'plan', 'content': 'task steps'}, 'status': 'completed'},
        {'type': 'mcpToolCall', 'tool': 'Scratchpad', 'arguments': {'section': 'failed', 'content': 'not saved'}, 'status': 'failed'},
        {'type': 'commandExecution', 'command': 'false', 'exitCode': 1},
    ]
    client = _ScriptedClient(events=[{'method': 'item/completed', 'params': {'item': item}} for item in items] + complete_events())
    evidence, scratch, finalized = [], [], []
    await helper_session(client).run_turn('x', model='gpt-5.6-sol', effort='low', developer_instructions='safe',
        tool_result_callback=lambda *args: evidence.append(args),
        scratch_stream_callback=lambda *args: scratch.append(args),
        scratch_finalize_callback=lambda: finalized.append(True))
    assert [call[3] for call in evidence] == [False, True, True]
    assert scratch == [('plan', 'task steps', 'write')] and finalized == [True]


@pytest.mark.asyncio
async def test_auxiliary_uses_exact_model_no_native_authority_and_closes(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, 'STATE_DIR', str(tmp_path))
    monkeypatch.setattr(config, 'CODEX_APP_SERVER_MODEL', 'gpt-6-astra')
    monkeypatch.setattr('services.codex_app_server.manager._global_codex_config',
                        lambda: {'mcp_servers': {'personal-mail': {}}})
    session = SimpleNamespace(run_turn=AsyncMock(return_value=result()), close=AsyncMock())
    factory = Mock(return_value=session)
    monkeypatch.setattr(auxiliary, 'CodexSession', factory)
    await auxiliary.run_task(prompt='task', instructions='helper', tools=[{'name': 'Read'}], execute_tool=AsyncMock())
    options = factory.call_args.kwargs
    assert options['sandbox'] == 'read-only' and options['approval_policy'] == 'never'
    assert options['ephemeral'] and not options['allow_host_approvals'] and not options['allow_local_images']
    assert 'features.shell_tool=false' in options['extra_args']
    assert 'mcp_servers.personal-mail.enabled=false' in options['extra_args']
    assert 'features.code_mode_host=true' in options['extra_args']
    assert session.run_turn.await_args.kwargs['model'] == 'gpt-6-astra'
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_auxiliary_cancellation_closes_session(monkeypatch, tmp_path):
    monkeypatch.setattr('config.STATE_DIR', str(tmp_path))
    monkeypatch.setattr(auxiliary, 'isolated_args', lambda: [])
    session = SimpleNamespace(run_turn=AsyncMock(side_effect=asyncio.CancelledError), close=AsyncMock())
    monkeypatch.setattr(auxiliary, 'CodexSession', lambda **_: session)
    with pytest.raises(asyncio.CancelledError):
        await auxiliary.run_task(prompt='task', instructions='helper')
    session.close.assert_awaited_once()


def test_semaphores_are_local_to_each_event_loop():
    async def inspect():
        first = auxiliary._semaphore()
        assert first is auxiliary._semaphore()
        return first
    assert asyncio.run(inspect()) is not asyncio.run(inspect())


def test_usage_preserves_cached_tokens_without_double_counting():
    usage = auxiliary.normalized_usage(replace(result(), token_usage_last={
        'inputTokens': 150, 'cachedInputTokens': 100, 'outputTokens': 30}))
    assert usage == {'input_tokens': 50, 'cache_read_input_tokens': 100,
                     'output_tokens': 30, 'cache_creation_input_tokens': 0}


@pytest.fixture
def deliberate(monkeypatch):
    import config
    monkeypatch.setattr(config, 'DELIB_PLANNER_ENABLED', False)
    monkeypatch.setattr(config, 'MAGI_LITE_ENABLED', False)
    monkeypatch.setattr(config, 'DELIB_VERIFIER_ENABLED', True)
    monkeypatch.setattr(config, 'DELIB_VERIFY_MAX_REVISIONS', 1)
    evidence = SimpleNamespace(records=[], digest=lambda **_: '(no tool calls)')
    execute = AsyncMock(side_effect=[result('draft'), result('revised'), result('third')])
    return {'prompt': 'task', 'user_request': 'task', 'model': 'gpt-6-astra',
            'channel_id': 10, 'execute': execute, 'route_judgment': False, 'evidence': evidence}


@pytest.mark.asyncio
@pytest.mark.parametrize(('verdicts', 'expected', 'calls'), [
    (['{"pass":true,"issues":[]}'], 'draft', 1),
    (['{"pass":false,"issues":[{"claim":"42","problem":"wrong"}]}', '{"pass":true,"issues":[]}'], 'revised', 2),
    (['not json'], '自動検証の結果を確認できていません', 1),
    ([TimeoutError('timeout')], '自動検証を完了できていません', 1),
    (['{"pass":false,"issues":[{"claim":"42","problem":"wrong"}]}'] * 2, '未解決指摘', 2),
])
async def test_deliberate_review_and_bounded_revision(monkeypatch, deliberate, verdicts, expected, calls):
    helper = AsyncMock(side_effect=[value if isinstance(value, Exception) else result(value) for value in verdicts])
    monkeypatch.setattr(auxiliary, 'run_task', helper)
    output = await deliberation.run(**deliberate)
    assert expected in output.final_text
    assert deliberate['execute'].await_count == calls
    assert helper.await_args.kwargs['tools'][0]['name'] == 'Read'


@pytest.mark.asyncio
async def test_verifier_reads_only_current_evidence_and_bounded_regular_files(monkeypatch, deliberate, tmp_path):
    allowed = tmp_path / 'evidence.txt'
    allowed.write_text('e' * 210000)
    forbidden = tmp_path / 'secret.txt'
    forbidden.write_text('private')
    deliberate['evidence'].records = [SimpleNamespace(raw_path=str(allowed))]
    async def verify(**kwargs):
        read = kwargs['execute_tool']
        assert len(await read('Read', {'file_path': str(allowed)})) == 200000
        with pytest.raises(ValueError):
            await read('Read', {'file_path': str(forbidden)})
        with pytest.raises(ValueError):
            await read('Bash', {'file_path': str(allowed)})
        allowed.unlink()
        allowed.symlink_to(forbidden)
        with pytest.raises(ValueError):
            await read('Read', {'file_path': str(allowed)})
        return result('{"pass":true,"issues":[]}')
    monkeypatch.setattr(auxiliary, 'run_task', verify)
    assert (await deliberation.run(**deliberate)).final_text == 'draft'


@pytest.mark.asyncio
async def test_deliberate_cancellation_propagates(monkeypatch, deliberate):
    monkeypatch.setattr(auxiliary, 'run_task', AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await deliberation.run(**deliberate)


@pytest.mark.asyncio
async def test_failed_revision_preserves_draft_with_unresolved_caveat(monkeypatch, deliberate):
    monkeypatch.setattr(auxiliary, 'run_task', AsyncMock(return_value=result(
        '{"pass":false,"issues":[{"claim":"42","problem":"incorrect"}]}')))
    deliberate['execute'] = AsyncMock(side_effect=[result('original draft'), RuntimeError('revision unavailable')])
    output = await deliberation.run(**deliberate)
    assert output.final_text.startswith('original draft') and '未解決指摘' in output.final_text


@pytest.mark.asyncio
async def test_long_tool_backed_chat_judge_appends_caveat_without_revision(monkeypatch):
    from services.claude_api.verifier import Verdict, VerifierIssue
    monkeypatch.setattr('config.DELIB_JUDGE_LITE_ENABLED', True)
    monkeypatch.setattr('services.claude_api.verifier.DELIB_JUDGE_MIN_CHARS', 1)
    judge = AsyncMock(return_value=Verdict(ok=False, issues=[VerifierIssue('42', 'incorrect')]))
    monkeypatch.setattr('services.claude_api.verifier.judge_lite', judge)
    evidence = SimpleNamespace(records=[object()], digest=lambda: 'real evidence')
    emitted = []
    output = await deliberation.review_chat(result=result('draft'), user_request='question', channel_id=10,
        evidence=evidence, tier='chat', incremental_text_callback=emitted.append)
    assert output.final_text.startswith('draft') and '未解決指摘' in output.final_text
    assert emitted and emitted[0] in output.final_text
    judge.assert_awaited_once()


@pytest.mark.asyncio
async def test_entry_records_failure_and_revision_turn_without_streaming_drafts(monkeypatch):
    monkeypatch.setenv('BOT_AUX_LLM_PROVIDER', 'codex')
    monkeypatch.setattr(entry, 'CODEX_APP_SERVER_ENABLED', True)
    monkeypatch.setattr(entry, 'append_jsonl', lambda *_args, **_kwargs: None)
    evidence = SimpleNamespace(add=AsyncMock(), next_seq=lambda: 1)
    monkeypatch.setattr('services.claude_api.evidence.new_index', lambda _: evidence)
    hook_states = []
    async def execute(_channel, _user, prompt, **kwargs):
        assert kwargs['text_stream_callback'] is None
        assert kwargs['incremental_text_callback'] is None
        hook_states.append(kwargs['turn_hooks'])
        await kwargs['tool_result_callback']('Bash', {}, 'failed', prompt == 'first')
        return result(prompt)
    manager = SimpleNamespace(has_thread=lambda *_: False, has_live_session=lambda *_: False, run_turn=execute)
    monkeypatch.setattr(entry, 'get_manager', lambda: manager)
    async def deliberate(**kwargs):
        await kwargs['execute']('first')
        return await kwargs['execute']('revision')
    monkeypatch.setattr(deliberation, 'run', deliberate)
    output = await entry.call_with_tools('task', 10, {}, user_id=20, tier='deliberate',
        text_stream_callback=Mock(), incremental_text_callback=Mock())
    assert output == ('revision', None, False)
    assert len(hook_states) == 2 and hook_states[0] is hook_states[1]
    assert [(call.kwargs['turn'], call.kwargs['is_error']) for call in evidence.add.await_args_list] == [(1, True), (2, False)]


@pytest.mark.asyncio
async def test_deliberate_planner_and_magi_feed_revision_and_scratch(monkeypatch, deliberate):
    from services.claude_api.planner import Plan
    monkeypatch.setattr('config.DELIB_PLANNER_ENABLED', True)
    monkeypatch.setattr('config.MAGI_LITE_ENABLED', True)
    monkeypatch.setattr('config.DELIB_VERIFIER_ENABLED', False)
    monkeypatch.setattr('services.claude_api.planner.generate_plan', AsyncMock(return_value=Plan(goal='check requirements')))
    monkeypatch.setattr('services.claude_api.magi_lite.run_lenses', AsyncMock(return_value=SimpleNamespace(degraded=False)))
    monkeypatch.setattr('services.claude_api.magi_lite.build_lens_revise_prompt', lambda _: 'independent critique')
    scratch, finalized = [], []
    output = await deliberation.run(**{**deliberate, 'route_judgment': True},
        scratch_stream_callback=lambda *args: scratch.append(args),
        scratch_finalize_callback=lambda: finalized.append(True))
    assert output.final_text == 'revised'
    assert 'check requirements' in deliberate['execute'].await_args_list[0].args[0]
    assert deliberate['execute'].await_args_list[1].args[0] == 'independent critique'
    assert scratch[0][0] == '計画' and scratch[0][2] == 'write' and finalized == [True]


@pytest.mark.asyncio
async def test_restricted_codex_never_creates_admin_manager(monkeypatch):
    import config
    monkeypatch.setenv('CODEX_RESTRICTED_ENABLED', 'true')
    monkeypatch.setattr(entry, 'CODEX_APP_SERVER_ENABLED', True)
    monkeypatch.setattr(entry, 'get_manager', lambda: pytest.fail('restricted cannot create native admin manager'))
    helper = AsyncMock(return_value=result('restricted answer'))
    monkeypatch.setattr(auxiliary, 'run_task', helper)
    assert await entry.call_with_tools('user task', 10, {}, restricted=True) == ('restricted answer', None, False)
    assert helper.await_args.kwargs['instructions'] == config.RESTRICTED_SYSTEM_PROMPT
    assert 'tools' not in helper.await_args.kwargs


def test_restricted_provider_routes_only_when_safe_codex_path_enabled(monkeypatch):
    from services.providers.policy import resolve_provider_name
    monkeypatch.setenv('CODEX_RESTRICTED_ENABLED', 'true')
    assert resolve_provider_name(explicit='claude', model=None, restricted=True, default='codex') == 'codex'
    assert resolve_provider_name(explicit='codex', model=None, restricted=True, default='claude') == 'claude'
    monkeypatch.delenv('CODEX_RESTRICTED_ENABLED')
    assert resolve_provider_name(explicit='codex', model=None, restricted=True, default='codex') == 'claude'


def test_codex_only_readiness_does_not_require_unused_claude(monkeypatch):
    from services import health
    monkeypatch.setenv('DEFAULT_LLM_PROVIDER', 'codex')
    monkeypatch.setenv('BOT_AUX_LLM_PROVIDER', 'codex')
    monkeypatch.setenv('CODEX_RESTRICTED_ENABLED', 'true')
    for name in ['discord', 'mem0', 'system']:
        monkeypatch.setattr(health, f'_check_{name}', lambda *_: {'ok': True})
    monkeypatch.setattr(health, '_check_release', lambda: {'ok': True, 'sha': 'a' * 40})
    monkeypatch.setattr(health, '_check_oauth', lambda: {'ok': False, 'reason': 'expired'})
    monkeypatch.setattr(readiness, 'check', lambda: {'ok': True})
    snapshot = health.get_readiness_snapshot()
    assert snapshot['status'] == 'ok'
    assert snapshot['subsystems']['oauth']['required'] is False
    monkeypatch.setattr(readiness, 'check', lambda: {'ok': False})
    assert health.get_readiness_snapshot()['status'] == 'degraded'
    monkeypatch.delenv('BOT_AUX_LLM_PROVIDER')
    assert readiness.claude_required()


@pytest.mark.asyncio
async def test_codex_bootstrap_does_not_refresh_claude(monkeypatch):
    import main_async
    monkeypatch.setattr(readiness, 'claude_required', lambda: False)
    monkeypatch.setattr(readiness, 'check', lambda: {'ok': True})
    monkeypatch.setattr('services.oauth_manager.init', lambda: pytest.fail('unused Claude auth must not initialize'))
    assert await main_async._bootstrap_oauth()

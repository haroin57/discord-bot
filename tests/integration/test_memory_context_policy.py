"""Real SQLite/registry/Codex mapping lifecycle without launching an LLM."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.memory_policy import MemoryPolicy
from services.conversation_policy import (
    ConversationPolicyRegistry, filter_rows, filter_memories, retire_native_context,
    sanitize_derived,
)
from services.work_context import current_conversation


async def test_scope_change_retires_persisted_native_thread_and_rebuilds_allowed_sources(tmp_path, monkeypatch):
    from services.codex_app_server import manager as manager_module
    from services import session
    from services.claude_api import session_store

    policy_path = tmp_path / 'policy.sqlite3'
    policy = MemoryPolicy(policy_path)
    record = policy.remember('限定する内容', '1', source_message_id='source').record_ids[0]
    registry_path = tmp_path / 'contexts.sqlite3'
    registry = ConversationPolicyRegistry(registry_path, policy)
    before = registry.begin('1', '100', False)
    registry.commit(before)
    audience = policy.audience_key('1', '100', False)

    manager = manager_module.CodexSessionManager(state_path=str(tmp_path / 'native.json'))
    manager._state['100:1'] = {'thread_id': 'native-containing-old-facts'}
    running = SimpleNamespace(close=AsyncMock())
    manager._sessions['100:1'] = running
    await manager._save_state()
    monkeypatch.setattr(manager_module, '_manager', manager)
    clear_session = Mock()
    monkeypatch.setattr(session, 'clear_session', clear_session)
    monkeypatch.setattr(session_store, '_session_timestamps', {})
    monkeypatch.setattr(session_store, '_session_sticky_model', {})
    session_store._record_session_time(100, 1)
    assert session_store._is_session_fresh(100, 1)

    policy.share(record, '1', 'private')
    restarted_policy = MemoryPolicy(policy_path)
    monkeypatch.setattr('services.memory_policy.get_memory_policy', lambda: restarted_policy)
    restarted_registry = ConversationPolicyRegistry(registry_path, restarted_policy)
    after = restarted_registry.begin('1', '100', False)
    assert after.rebuild
    await retire_native_context(100, 1)
    running.close.assert_awaited_once()
    clear_session.assert_called_once_with(100, 1)
    assert not manager_module.CodexSessionManager(state_path=manager.state_path).has_thread(100, 1)
    assert not session_store._is_session_fresh(100, 1)

    token = current_conversation.set(after)
    try:
        rows = [
            {'role': 'user', 'message_id': 'source', 'content': '非公開内容の別表現'},
            {'role': 'user', 'message_id': 'allowed', 'author_id': '2', 'content': '別チャンネルの認可済み相談'},
            {'role': 'assistant', 'content': '古い回答の言い換え', 'memory_epoch': before.epoch, 'audience_key': audience},
            {'role': 'user', 'is_compact_summary': True, 'content': '古い要約の言い換え'},
        ]
        assert [r['message_id'] for r in filter_rows(rows)] == ['allowed']
        assert not filter_memories([{'id': record, 'memory': '限定する内容', 'user_id': '1'}])
        assert sanitize_derived('旧成果物の言い換え', recorded_epoch=before.epoch) == ''
        restarted_registry.commit(after)
        assert not restarted_registry.begin('1', '100', False).rebuild
        assert restarted_registry.begin('1', '101', False).rebuild
    finally:
        current_conversation.reset(token)


def test_output_and_summary_cannot_commit_after_concurrent_correction(tmp_path):
    policy = MemoryPolicy(tmp_path / 'policy.sqlite3')
    record = policy.remember('旧条件', '1').record_ids[0]
    registry = ConversationPolicyRegistry(tmp_path / 'contexts.sqlite3', policy)
    in_flight = registry.begin('1', '100', False)
    policy.correct(record, '1', replacement='新条件')
    assert not policy.is_current(in_flight.epoch)
    with pytest.raises(RuntimeError):
        registry.commit(in_flight)
    assert registry.begin('1', '100', False).rebuild


def test_history_reads_revision_once_and_refreshes_after_scope_change(tmp_path, monkeypatch):
    policy = MemoryPolicy(tmp_path / "policy.sqlite3")
    record = policy.remember("restricted original", "1").record_ids[0]
    registry = ConversationPolicyRegistry(tmp_path / "contexts.sqlite3", policy)
    context = registry.begin("1", "100", False)
    audience = policy.audience_key("1", "100", False)
    rows = [{"role": "assistant", "content": f"derived answer {index}",
             "memory_epoch": context.epoch, "audience_key": audience} for index in range(40)]
    rows.append({"role": "assistant", "content": "another audience",
                 "memory_epoch": context.epoch, "audience_key": policy.audience_key("2", "100", False)})
    epoch = Mock(wraps=policy.epoch)
    monkeypatch.setattr(policy, "epoch", epoch)
    monkeypatch.setattr("services.memory_policy.get_memory_policy", lambda: policy)
    token = current_conversation.set(context)
    try:
        assert filter_rows(rows) == rows[:-1]
        assert epoch.call_count == 1
        policy.share(record, "1", "private")
        assert not filter_rows(rows)
        assert epoch.call_count == 2
    finally:
        current_conversation.reset(token)


def test_registry_begins_with_one_consistent_revision(tmp_path, monkeypatch):
    policy = MemoryPolicy(tmp_path / "policy.sqlite3")
    registry = ConversationPolicyRegistry(tmp_path / "contexts.sqlite3", policy)
    prior = registry.begin("1", "100", False)
    registry.commit(prior)
    epoch = Mock(wraps=policy.epoch)
    monkeypatch.setattr(policy, "epoch", epoch)
    current = registry.begin("1", "100", False)
    assert not current.rebuild
    assert current.epoch == prior.epoch
    assert epoch.call_count == 1

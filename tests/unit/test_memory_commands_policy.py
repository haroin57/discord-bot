from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from bot_commands import memory
from services.memory_policy import MemoryPolicy


@pytest.fixture
def command_setup(tmp_path, monkeypatch):
    policy = MemoryPolicy(tmp_path / 'policy.sqlite3')
    monkeypatch.setattr(memory, 'get_memory_policy', lambda: policy)
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    deps = SimpleNamespace(is_admin=lambda user: user.id == 1)
    memory.register(tree, deps)
    return tree, policy


def interaction(user_id=1, guild_id=10):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), id=900, channel_id=100,
                           guild_id=guild_id, created_at=datetime.now(timezone.utc),
                           response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


async def test_all_commands_registered_and_nonadmin_is_not_promoted(command_setup):
    tree, policy = command_setup
    assert {c.name for c in tree.get_commands()} == {'remember', 'recall', 'forget', 'correction', 'share', 'preferences'}
    msg = interaction(2)
    await tree.get_command('remember').callback(msg, text='other owner fact')
    assert not policy.inspect_records('2')
    msg.response.send_message.assert_awaited_once()
    assert msg.response.send_message.call_args.kwargs['ephemeral'] is True


async def test_real_commands_save_correct_share_preferences_privately(command_setup):
    tree, policy = command_setup
    msg = interaction()
    await tree.get_command('remember').callback(msg, text='本人限定の内容', scope='private')
    rows = policy.inspect_records('1')
    assert len(rows) == 1
    assert not policy.recall('1', is_private=False)
    record_id = rows[0]['id']
    await tree.get_command('correction').callback(msg, record_id, 'wrong', replacement='訂正後の内容', subject='1:father')
    new = policy.inspect_records('1')[0]
    assert new['metadata']['subject_id'] == '1:father'
    await tree.get_command('share').callback(msg, new['id'], 'shared')
    assert policy.recall('1')
    await tree.get_command('preferences').callback(msg, key='hide_name', value='true')
    assert policy.preferences('1', '100', False)['hide_name'] == 'true'
    for call in msg.followup.send.call_args_list:
        assert call.kwargs['ephemeral'] is True
    assert '本人限定の内容' not in str(msg.followup.send.call_args_list)


async def test_future_case_memory_is_inspectable_by_owner(command_setup, monkeypatch):
    tree, policy = command_setup
    record_id = policy.remember('将来案件の条件', '1', scope='case', case_id='case', valid_from=9999999999).record_ids[0]
    # Isolate the unavailable optional legacy backend without making network calls.
    import services.mem0
    monkeypatch.setattr(services.mem0, 'get_all_memories', AsyncMock(return_value=[]))
    msg = interaction()
    await tree.get_command('recall').callback(msg)
    assert record_id in str(msg.followup.send.call_args_list)
    assert '対象' in str(msg.followup.send.call_args_list)
    assert not policy.recall('1')


async def test_failed_legacy_read_is_not_reported_as_empty_success(command_setup, monkeypatch):
    tree, policy = command_setup
    import services.mem0
    monkeypatch.setattr(services.mem0, 'get_all_memories', AsyncMock(side_effect=RuntimeError('secret backend detail')))
    msg = interaction()
    await tree.get_command('recall').callback(msg)
    output = str(msg.followup.send.call_args_list)
    assert '取得は未完了' in output
    assert 'secret backend detail' not in output


async def test_forget_selector_rechecks_owner(command_setup):
    tree, policy = command_setup
    saved = policy.remember('delete only mine', '1')
    view = memory._ForgetView(policy.inspect_records('1'), '1')
    msg = interaction(2)
    await view.children[0].callback(msg)
    assert policy.get('1', saved.record_ids[0])['status'] == 'active'
    msg.response.send_message.assert_awaited_once_with('本人のみ操作できます。', ephemeral=True)


async def test_forged_foreign_id_cannot_be_adopted(command_setup, monkeypatch):
    tree, policy = command_setup
    import services.mem0
    monkeypatch.setattr(services.mem0, 'get_all_memories', AsyncMock(return_value=[{'id': 'foreign', 'user_id': '2', 'memory': 'private'}]))
    with pytest.raises(ValueError):
        await memory._ensure_owned(policy, '1', 'foreign')
    assert not policy.inspect_records('1')


async def test_forget_reports_partial_deletion_without_claiming_all_saved_data(command_setup):
    tree, policy = command_setup
    record_id = policy.remember('delete fact', '1').record_ids[0]
    msg = interaction()
    await tree.get_command('forget').callback(msg, record_id=record_id)
    output = str(msg.followup.send.call_args_list)
    assert '全保存先削除は完了していません' in output
    assert policy.get('1', record_id)['text'] == ''

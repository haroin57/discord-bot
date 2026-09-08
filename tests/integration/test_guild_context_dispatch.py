"""Gateway-to-model coverage with local context storage and no external I/O."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import bot
    from services import guild_context as gc

    monkeypatch.setenv('GUILD_CONTEXT_ENABLED', 'true')
    store = gc.GuildContextStore(tmp_path / 'guild.sqlite')
    monkeypatch.setattr(gc, 'get_store', lambda: store)
    monkeypatch.setattr(gc, '_hydrated', {})
    monkeypatch.setattr(gc, '_hydrate_locks', {})
    monkeypatch.setattr(gc, '_store_lock', asyncio.Lock())
    monkeypatch.setattr(gc, '_hydrate_visible', AsyncMock())

    resources = MagicMock()
    resources.accepting_jobs = True
    resources.register_dispatch_task.return_value = True
    resources.release_renderer = AsyncMock()
    monkeypatch.setattr(bot, '_runtime_resources', resources)
    monkeypatch.setattr(bot, '_turn_outbox', MagicMock())
    bot._turn_outbox.snapshot.return_value = None
    status = MagicMock()
    for name in ('initial_reply', 'update', 'close', 'edit_final'):
        setattr(status, name, AsyncMock())
    monkeypatch.setattr(bot.bot_ui, 'StatusController', lambda *args, **kwargs: status)
    ticket = NS(position=1, wait=AsyncMock(), release=MagicMock())
    monkeypatch.setattr(bot, '_get_channel_sema', lambda cid: NS(issue=lambda: ticket))
    monkeypatch.setattr(bot, 'cleanup_attachments', MagicMock())
    monkeypatch.setattr(bot, 'resolve_provider', lambda **kwargs: 'codex')
    resume_calls = []
    def resume(provider, cid, uid):
        resume_calls.append((provider, cid, uid))
        return True
    monkeypatch.setattr(bot, 'provider_resume_hint', resume)
    context = MagicMock(will_resume=True, conv_context='', text_attachments=[],
                        arbitrary_attachments=[], attachment_rejections=[])
    context.close = AsyncMock()
    monkeypatch.setattr(bot.bot_context, 'prepare_context', AsyncMock(return_value=context))
    monkeypatch.setattr(bot.bot_prompt, 'build_prompt', AsyncMock(return_value=NS(
        prompt='LEAN_CURRENT_USER_INPUT', active_skill_prompt='')))
    captured = []
    async def invoke(model_input, **kwargs):
        captured.append(model_input)
        return NS(response_text='', session_info=None, actually_resumed=True,
                  renderer=NS(live_sent_any=False))
    monkeypatch.setattr(bot.bot_model, 'invoke_model', invoke)
    monkeypatch.setattr(bot.bot_publish, 'prepare_response', AsyncMock(return_value=None))

    guild = NS(id=10)
    channel = NS(id=20, name='shared', guild=guild, send=AsyncMock(),
                 permissions_for=lambda member: NS(view_channel=True, read_message_history=True))
    monkeypatch.setattr(bot.bot, 'get_channel', lambda cid: channel if cid == 20 else None)
    return NS(bot=bot, gc=gc, store=store, captured=captured, resume_calls=resume_calls,
              guild=guild, channel=channel)


def message(runtime, mid, uid, text):
    return NS(id=mid, guild=runtime.guild, channel=runtime.channel,
              author=NS(id=uid, display_name=f'user-{uid}', bot=False),
              content=text, created_at=datetime.now(timezone.utc), edited_at=None,
              add_reaction=AsyncMock(), remove_reaction=AsyncMock())


@pytest.mark.asyncio
async def test_unaddressed_correction_reaches_another_users_resumed_model(runtime, monkeypatch):
    bot = runtime.bot
    monkeypatch.setattr(bot.bot_admission, 'admit_message', AsyncMock(return_value=None))
    await bot.on_message(message(runtime, 100, 30, 'CORRECTION_FROM_OTHER_USER'))
    assert runtime.captured == []  # Receipt does not authorize a reply.

    monkeypatch.setattr(bot.bot_admission, 'admit_message', AsyncMock(return_value=NS(
        caller_is_admin=True, is_dm=False, raw_user_text='question')))
    await bot.on_message(message(runtime, 200, 31, 'question'))
    assert len(runtime.captured) == 1
    first = runtime.captured[0]
    assert 'CORRECTION_FROM_OTHER_USER' in first.prompt
    assert 'LEAN_CURRENT_USER_INPUT' in first.prompt
    assert first.message.author.id == 31
    assert first.caller_is_admin is True
    assert runtime.resume_calls == [('codex', 20, 31)]

    # Another receipt must be reflected even when the same native session resumes.
    monkeypatch.setattr(bot.bot_admission, 'admit_message', AsyncMock(return_value=None))
    await bot.on_message(message(runtime, 201, 32, 'NEWER_CONFIRMED_RESULT'))
    monkeypatch.setattr(bot.bot_admission, 'admit_message', AsyncMock(return_value=NS(
        caller_is_admin=False, is_dm=False, raw_user_text='next question')))
    await bot.on_message(message(runtime, 202, 31, 'next question'))
    assert 'NEWER_CONFIRMED_RESULT' in runtime.captured[-1].prompt
    assert runtime.captured[-1].caller_is_admin is False  # Shared data never grants tools.
    assert runtime.resume_calls[-1] == ('codex', 20, 31)


@pytest.mark.asyncio
async def test_raw_gateway_edit_delete_and_bulk_delete_hooks(runtime):
    bot, gc = runtime.bot, runtime.gc
    await gc.observe(message(runtime, 100, 30, 'before edit'))
    await bot.on_raw_message_edit(NS(guild_id=10, channel_id=20, message_id=100,
        data={'content': 'after edit', 'edited_timestamp': datetime.now(timezone.utc).isoformat()}))
    assert runtime.store.recent(10)[0]['content'] == 'after edit'
    await bot.on_raw_message_delete(NS(message_id=100))
    assert runtime.store.recent(10) == []
    await gc.observe(message(runtime, 101, 31, 'one'))
    await gc.observe(message(runtime, 102, 32, 'two'))
    await bot.on_raw_bulk_message_delete(NS(message_ids={101, 102}))
    assert runtime.store.recent(10) == []


@pytest.mark.asyncio
async def test_prompt_budget_is_respected_in_composition_root(runtime, monkeypatch):
    bot = runtime.bot
    monkeypatch.setattr(bot, 'MAX_PROMPT_CHARS', 750)
    await runtime.gc.observe(message(runtime, 100, 30, 'shared correction'))
    monkeypatch.setattr(bot.bot_admission, 'admit_message', AsyncMock(return_value=NS(
        caller_is_admin=True, is_dm=False, raw_user_text='question')))
    await bot.on_message(message(runtime, 200, 31, 'question'))
    assert runtime.captured
    assert len(runtime.captured[0].prompt) <= 750

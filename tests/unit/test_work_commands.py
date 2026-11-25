"""Slash persistence precedes acknowledgements, execution and private display."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def commands(tmp_path, monkeypatch):
    from bot_commands import work as module
    from services.work.store import WorkStore
    store = WorkStore(tmp_path / "work.sqlite3")
    runtime = SimpleNamespace(start=AsyncMock(), interrupt=Mock(), reconcile=AsyncMock(return_value="保存した結果を照合しました。"))
    monkeypatch.setitem(sys.modules, "services.work.runtime", SimpleNamespace(get_work_runtime=lambda: runtime))
    monkeypatch.setattr(module, "get_work_store", lambda: store)
    policy = SimpleNamespace(
        epoch=lambda: 7, is_current=lambda value: value == 7,
        can_reuse_context=lambda value, **kwargs: value == 7 and kwargs["stored_audience"] == "current-audience",
        sanitize_context=lambda text, *args, **kwargs: text,
    )
    monkeypatch.setattr(module, "_policy", lambda: policy)
    registered = []
    deps = SimpleNamespace(is_admin=lambda user: user.id in {10, 11}, bot=SimpleNamespace(get_user=lambda uid: SimpleNamespace(id=uid)))
    module.register(SimpleNamespace(add_command=registered.append), deps)
    callbacks = {command.name: command.callback for command in registered[0].commands}
    return SimpleNamespace(store=store, runtime=runtime, callbacks=callbacks, policy=policy, module=module)


def interaction(user_id=10, message_id=100):
    return SimpleNamespace(
        id=message_id, user=SimpleNamespace(id=user_id), channel_id=20, guild_id=30,
        channel=SimpleNamespace(id=20, recipient=None),
        response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def content(request):
    return "\n".join(call.args[0] for call in request.followup.send.call_args_list)


def create(commands, **fields):
    return commands.store.create("10", "20", "比較用件です。", "create-1", **fields)


async def test_create_commits_before_runtime_and_success_reply(commands):
    from services.work.store import WorkStore
    request = interaction()
    observed = []

    async def start(work_id, actor, channel):
        assert not request.followup.send.called
        saved = WorkStore(commands.store.path).get(work_id, actor)
        assert saved.goal == "翌日の比較です。"
        observed.append(saved.id)

    commands.runtime.start.side_effect = start
    await commands.callbacks["create"](request, "翌日の比較です。", "比較表を保存します。")
    assert observed and observed[0] in content(request)
    assert "保存しました" in content(request) and "実行開始を受け付けました" in content(request)
    assert request.followup.send.call_args.kwargs["ephemeral"] is True


async def test_registration_exposes_all_structured_operations(commands):
    assert set(commands.callbacks) == {"create", "list", "show", "amend", "pause", "resume", "cancel", "answer", "artifact", "share", "reconcile"}


async def test_non_admin_cannot_create_or_read(commands):
    request = interaction(user_id=99)
    await commands.callbacks["create"](request, "保存しない用件です。")
    assert commands.store.list_for("99") == []
    assert "admin only" in request.response.send_message.call_args.args[0]
    commands.runtime.start.assert_not_called()


async def test_failed_persistence_has_no_false_success_or_execution(commands, monkeypatch):
    from services.work.store import WorkStorageError
    monkeypatch.setattr(commands.store, "create", Mock(side_effect=WorkStorageError("保存できませんでした。")))
    request = interaction()
    await commands.callbacks["create"](request, "比較します。")
    assert "保存しました" not in content(request)
    assert "保存できませんでした" in content(request)
    commands.runtime.start.assert_not_called()


async def test_failed_runtime_keeps_saved_work_and_reports_no_start(commands):
    commands.runtime.start.side_effect = RuntimeError("provider unavailable")
    request = interaction()
    await commands.callbacks["create"](request, "比較します。")
    work = commands.store.list_for("10")[0]
    assert work.id in content(request) and "保存しました" in content(request)
    assert "実行開始の受付は確認できませんでした" in content(request)
    assert "実行開始を受け付けました" not in content(request)


async def test_pause_targets_only_authorized_work_and_interrupts_after_commit(commands):
    work = create(commands)
    other = commands.store.create("11", "20", "別人の用件です。", "create-2")
    commands.store.transition(work.id, "10", "running")
    commands.store.transition(other.id, "11", "running")

    def interrupt(work_id):
        assert commands.store.get(work_id).state == "paused"
        assert commands.store.get(other.id).state == "running"

    commands.runtime.interrupt.side_effect = interrupt
    request = interaction()
    await commands.callbacks["pause"](request, work.id)
    commands.runtime.interrupt.assert_called_once_with(work.id)
    intruder = interaction(user_id=11, message_id=101)
    await commands.callbacks["cancel"](intruder, work.id)
    assert "権限がありません" in content(intruder)
    assert commands.store.get(work.id).state == "paused"


async def test_completed_resume_is_queued_before_start(commands):
    work = create(commands)
    commands.store.transition(work.id, "10", "running")
    commands.store.transition(work.id, "10", "completed")

    async def start(work_id, actor, channel):
        assert commands.store.get(work_id).state == "queued"

    commands.runtime.start.side_effect = start
    request = interaction()
    await commands.callbacks["resume"](request, work.id)
    commands.runtime.start.assert_awaited_once()


async def test_cancelled_resume_does_not_start(commands):
    work = create(commands)
    commands.store.transition(work.id, "10", "cancelled")
    request = interaction()
    await commands.callbacks["resume"](request, work.id)
    commands.runtime.start.assert_not_called()
    assert "処理できませんでした" in content(request)


async def test_pause_cannot_clear_reconciliation_requirement(commands):
    work = create(commands)
    commands.store.transition(work.id, "10", "needs_reconciliation")
    request = interaction()
    await commands.callbacks["pause"](request, work.id)
    assert commands.store.get(work.id).state == "needs_reconciliation"
    assert "/work reconcile" in content(request)
    commands.runtime.interrupt.assert_not_called()


async def test_reconcile_calls_runtime_after_authorization_and_returns_result(commands):
    work = create(commands)
    request = interaction()
    await commands.callbacks["reconcile"](request, work.id)
    commands.runtime.reconcile.assert_awaited_once_with(work.id, "10", request.channel)
    assert content(request) == "保存した結果を照合しました。"
    assert request.followup.send.call_args.kwargs["ephemeral"] is True
    intruder = interaction(user_id=11, message_id=101)
    await commands.callbacks["reconcile"](intruder, work.id)
    assert "権限がありません" in content(intruder)
    assert commands.runtime.reconcile.await_count == 1


async def test_reconcile_failure_does_not_claim_success(commands):
    work = create(commands)
    commands.runtime.reconcile.side_effect = RuntimeError("history unavailable")
    request = interaction()
    await commands.callbacks["reconcile"](request, work.id)
    assert "照合しました" not in content(request)
    assert "処理結果を確認できませんでした" in content(request)


async def test_question_answer_is_durable_before_start(commands):
    work = create(commands)
    question = commands.store.add_question(work.id, "予算を指定してください。")
    commands.store.transition(work.id, "10", "waiting")

    async def start(work_id, actor, channel):
        assert commands.store.list_questions(work_id)[0]["answer"] == "8万円"

    commands.runtime.start.side_effect = start
    request = interaction()
    await commands.callbacks["answer"](request, work.id, question["id"], "8万円")
    assert "回答を保存しました" in content(request)
    commands.runtime.start.assert_awaited_once()


@pytest.mark.parametrize("metadata", [{}, {"memory_epoch": 6, "audience_key": "current-audience"}, {"memory_epoch": 7, "audience_key": "old-audience"}])
async def test_old_or_unversioned_artifact_is_hidden_even_ephemerally(commands, metadata):
    work = create(commands)
    artifact = commands.store.add_artifact(work.id, expected_revision=work.revision, source_id="report", body="秘密の旧本文", title="秘密の旧題名", metadata=metadata)
    request = interaction()
    await commands.callbacks["artifact"](request, work.id, artifact["id"])
    assert "秘密" not in content(request)
    assert "再構成が必要" in content(request)
    assert "file" not in request.followup.send.call_args.kwargs
    show = interaction(message_id=101)
    await commands.callbacks["show"](show, work.id)
    assert "秘密" not in content(show)
    assert "表示を保留" in content(show)


async def test_current_artifact_uses_stable_id_attachment(commands):
    work = create(commands)
    artifact = commands.store.add_artifact(work.id, expected_revision=work.revision, source_id="report", body="現在の比較表です。", metadata={"memory_epoch": 7, "audience_key": "current-audience"})
    request = interaction()
    attached = []

    async def send(text, **kwargs):
        if "file" in kwargs:
            attached.append((kwargs["file"].filename, kwargs["file"].fp.read()))

    request.followup.send.side_effect = send
    await commands.callbacks["artifact"](request, work.id, artifact["id"])
    assert attached == [(f"{artifact['id']}-v1.md", "現在の比較表です。".encode())]


async def test_private_then_shared_artifact_never_revives(commands):
    work = create(commands)
    artifact = commands.store.add_artifact(work.id, expected_revision=work.revision, source_id="report", body="旧本文", metadata={"memory_epoch": 7, "audience_key": "current-audience"})
    commands.store.amend(work.id, "10", "", "private", audience="owner")
    commands.store.amend(work.id, "10", "", "shared", audience="shared")
    request = interaction()
    await commands.callbacks["artifact"](request, work.id, artifact["id"], 1)
    assert "再構成が必要" in content(request)
    assert "file" not in request.followup.send.call_args.kwargs


async def test_work_show_sanitizes_opaque_text(commands):
    work = create(commands, progress=["失効した個人情報です。"])
    commands.policy.sanitize_context = lambda *args, **kwargs: ""
    request = interaction()
    await commands.callbacks["show"](request, work.id)
    assert work.id in content(request)
    assert "失効した個人情報" not in content(request)
    assert "再構成が必要" in content(request)


async def test_epoch_change_during_render_blocks_old_command_output(commands):
    work = create(commands)
    epoch = [7]
    commands.policy.epoch = lambda: epoch[0]
    commands.policy.is_current = lambda value: value == epoch[0]

    def sanitize(text, *args, **kwargs):
        epoch[0] = 8
        return "古い秘密です。"

    commands.policy.sanitize_context = sanitize
    request = interaction()
    await commands.callbacks["show"](request, work.id)
    assert "古い秘密" not in content(request)
    assert "表示を再取得" in content(request)


async def test_share_rejects_non_admin_participants(commands):
    from discord import app_commands
    work = create(commands)
    request = interaction()
    await commands.callbacks["share"](request, work.id, app_commands.Choice(name="共有可", value="shared"), "99")
    assert commands.store.get(work.id).participants == []
    assert "確認済みの管理者ID" in content(request)

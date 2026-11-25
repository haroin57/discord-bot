"""User-visible work journeys across async changes and process reconstruction."""

import asyncio
from types import SimpleNamespace

import pytest


class Channel:
    def __init__(self, channel_id=10, owner=None):
        self.id = channel_id
        self.recipient = SimpleNamespace(id=owner) if owner else None
        self.messages = []

    async def send(self, content, **kwargs):
        message = SimpleNamespace(id=len(self.messages) + 100, content=content, **kwargs)
        self.messages.append(message)
        return message


def message(channel, text, *, uid=1, mid=1, reply=None):
    return SimpleNamespace(id=mid, channel=channel, author=SimpleNamespace(id=uid),
        guild=SimpleNamespace(id=30), content=text,
        reference=SimpleNamespace(message_id=reply) if reply else None)


@pytest.fixture
def store(tmp_path):
    from services.work.store import WorkStore
    return WorkStore(tmp_path / "work.sqlite3")


async def finish(execute, work, text="検証済みです。"):
    await execute("work_finish", {"complete": True, "criteria": [
        {"criterion": criterion, "satisfied": True, "evidence": text}
        for criterion in work.completion_criteria], "unknowns": [], "next_action": ""})


async def test_amendment_and_aside_do_not_wait_for_model_and_old_result_is_not_published(store):
    from services.work.runtime import WorkRuntime
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def model(**kwargs):
        calls.append(kwargs["work"])
        if len(calls) == 1:
            started.set()
            await release.wait()
            return "STALE_RESULT"
        assert "9万円以内に変更" in kwargs["prompt"]
        await finish(kwargs["execute"], kwargs["work"])
        return "9万円以内の候補を比較しました。"

    runtime = WorkRuntime(store, generate=model)
    channel = Channel()
    assert await runtime.handle_message(message(channel, "物件を比較して"), "物件を比較して", caller_is_admin=True)
    await started.wait()
    work = store.list_for("1")[0]
    assert not await runtime.handle_message(message(channel, "ところで2+2は", mid=2), "ところで2+2は", caller_is_admin=True)
    assert await runtime.handle_message(message(channel, "予算は9万円以内に変更", mid=3), "予算は9万円以内に変更", caller_is_admin=True)
    assert store.get(work.id).requirements["amendments"] == ["予算は9万円以内に変更"]
    release.set()
    await runtime._tasks[work.id]
    assert len(calls) == 2
    assert store.get(work.id).state == "completed"
    assert all("STALE_RESULT" not in item.content for item in channel.messages)
    assert "9万円以内" in channel.messages[-1].content


async def test_question_answer_after_restart_resumes_same_conditions(store):
    from services.work.runtime import WorkRuntime
    from services.work.store import WorkStore

    async def ask(**kwargs):
        await kwargs["execute"]("work_question", {"text": "徒歩上限を指定してください。", "required": True, "choices": ["10分", "15分"]})
        return "徒歩条件の回答を待っています。"

    channel = Channel()
    first = WorkRuntime(store, generate=ask)
    await first.handle_message(message(channel, "物件を比較して"), "物件を比較して", caller_is_admin=True)
    work = store.list_for("1")[0]
    await first._tasks[work.id]
    assert store.get(work.id).state == "waiting"
    question = store.list_questions(work.id, open_only=True)[0]
    question_message = first._question_message(work.id, question["id"])
    assert question_message
    restarted_store = WorkStore(store.path)
    restarted_store.recover()

    async def continue_model(**kwargs):
        assert "10分" in kwargs["prompt"]
        await finish(kwargs["execute"], kwargs["work"])
        return "徒歩10分以内で比較しました。"

    restarted = WorkRuntime(restarted_store, generate=continue_model)
    assert await restarted.handle_message(message(channel, "10分", mid=5, reply=int(question_message)), "10分", caller_is_admin=True)
    await restarted._tasks[work.id]
    assert restarted_store.get(work.id).state == "completed"
    assert restarted_store.list_questions(work.id)[0]["answer"] == "10分"


async def test_scope_change_during_execution_does_not_publish_in_old_channel(store):
    from services.work.runtime import WorkRuntime
    started, release = asyncio.Event(), asyncio.Event()

    async def model(**kwargs):
        started.set()
        await release.wait()
        return "本人限定の結果です。"

    channel = Channel()
    runtime = WorkRuntime(store, generate=model)
    work = store.create("1", "10", "比較して", "new")
    task = await runtime.start(work.id, "1", channel)
    await started.wait()
    store.amend(work.id, "1", "本人限定に変更", "private", audience="owner")
    release.set()
    await task
    assert all("本人限定の結果" not in item.content for item in channel.messages)
    assert store.get(work.id).state == "paused"


async def test_pause_before_start_task_runs_does_not_resume_itself(store):
    from services.work.runtime import WorkRuntime
    called = []

    async def model(**kwargs):
        called.append(True)
        return "unexpected"

    runtime = WorkRuntime(store, generate=model)
    work = store.create("1", "10", "比較して", "new")
    task = await runtime.start(work.id, "1", Channel())
    store.transition(work.id, "1", "paused")
    await task
    assert store.get(work.id).state == "paused"
    assert not called


async def test_ambiguous_owner_and_other_user_cannot_modify_wrong_work(store):
    from services.work.runtime import WorkRuntime

    async def model(**kwargs):
        raise AssertionError("must not execute")

    runtime = WorkRuntime(store, generate=model)
    one = store.create("1", "10", "Aを比較", "one")
    two = store.create("1", "10", "Bを比較", "two")
    channel = Channel()
    assert await runtime.handle_message(message(channel, "予算は8万", mid=9), "予算は8万", caller_is_admin=True)
    assert store.get(one.id).revision == store.get(two.id).revision == 1
    assert await runtime.handle_message(message(channel, "cancel", uid=2, mid=10), f"work {one.id} cancel", caller_is_admin=True)
    assert store.get(one.id).state == "queued"


async def test_discord_timeout_is_not_replayed_after_restart(store):
    from services.work.runtime import WorkRuntime
    from services.work.store import WorkStore

    async def model(**kwargs):
        await finish(kwargs["execute"], kwargs["work"])
        return "result"

    class UncertainChannel(Channel):
        async def send(self, content, **kwargs):
            if "成果物" in content:
                raise TimeoutError("ack lost")
            return await super().send(content, **kwargs)

    runtime = WorkRuntime(store, generate=model)
    work = store.create("1", "10", "compare", "new")
    await (await runtime.start(work.id, "1", UncertainChannel()))
    assert store.get(work.id).state == "needs_reconciliation"
    restored = WorkStore(store.path)
    restored.recover()
    restarted = WorkRuntime(restored, generate=model)
    with pytest.raises(ValueError):
        await restarted.start(work.id, "1", Channel())


async def test_smalltalk_creates_no_work(store):
    from services.work.runtime import WorkRuntime

    async def model(**kwargs):
        raise AssertionError("must not execute")

    runtime = WorkRuntime(store, generate=model)
    assert not await runtime.handle_message(message(Channel(), "おはよう"), "おはよう", caller_is_admin=True)
    assert store.list_for("1") == []

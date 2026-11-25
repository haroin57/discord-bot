"""Actual work composition with SQLite and local fake Discord/model boundaries."""

import asyncio
from types import SimpleNamespace

import pytest


class Channel:
    def __init__(self, channel_id, *, guild_id=10, recipient=None, can_view=True):
        self.id = channel_id
        self.recipient = SimpleNamespace(id=recipient) if recipient is not None else None
        self.can_view = can_view
        self.guild = None if recipient is not None else SimpleNamespace(
            id=guild_id, get_member=lambda actor: SimpleNamespace(id=actor),
        )
        self.sent = []

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=self.can_view, send_messages=True)

    async def send(self, content=None, **kwargs):
        message = SimpleNamespace(
            id=1000 + len(self.sent), content=content,
            author=SimpleNamespace(id=900), embeds=[kwargs["embed"]] if "embed" in kwargs else [],
            **{key: value for key, value in kwargs.items() if key != "embed"},
        )
        self.sent.append(message)
        return message

    async def history(self, **kwargs):
        for message in self.sent:
            yield message


class Client:
    def __init__(self):
        self.channels = {
            101: Channel(101), 102: Channel(102), 103: Channel(103, can_view=False),
            104: Channel(104, guild_id=20), 201: Channel(201, recipient=1),
            202: Channel(202, recipient=2),
        }
        self.user = SimpleNamespace(id=900)
        self.views = []

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise AssertionError("bootstrap must not fetch live Discord channels")

    def add_view(self, view, *, message_id):
        self.views.append((message_id, view))


@pytest.fixture
def composed(tmp_path, monkeypatch):
    import config
    from services import memory_policy
    from services.work import bootstrap, store as store_module, runtime as runtime_module
    from bot_commands import watch

    store = store_module.WorkStore(tmp_path / "work.sqlite3")
    policy = memory_policy.MemoryPolicy(tmp_path / "memory.sqlite3")
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(store_module, "_default_store", store)
    monkeypatch.setattr(memory_policy, "get_memory_policy", lambda: policy)
    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(watch, "_service", None)
    monkeypatch.setattr(watch, "_authorize_watch", None)
    bot = Client()
    tasks = []

    def task_factory(coroutine, **kwargs):
        task = asyncio.create_task(coroutine, **kwargs)
        tasks.append(task)
        return task

    def build():
        return bootstrap.build_runtime(bot=bot, task_factory=task_factory,
                                       provider_options=lambda *_: {"provider": "codex", "model": "test-model"})

    return SimpleNamespace(store=store, policy=policy, bot=bot, build=build,
                           bootstrap=bootstrap, tasks=tasks)


async def test_bootstrap_restores_question_views_and_durable_effect_recovery(composed):
    from services.work.effects import EffectExecutor, DiscordNotificationAdapter
    from bot_ui_work import WorkQuestionView
    from bot_commands import watch

    store, bot = composed.store, composed.bot
    work = store.create("1", "101", "Compare properties", "request-1", guild_id="10")
    question = store.add_question(work.id, "Select a budget", choices=["100", "200"])
    store.record_event(work.id, "question_displayed", {"question_id": question["id"], "message_id": "777"})
    work = store.transition(work.id, "1", "running")
    executor = EffectExecutor(store.path)
    executor.register_adapter("discord", DiscordNotificationAdapter(bot))
    grant = executor.authorize("1", "discord", "notify", "101", work_id=work.id, revision=work.revision)
    receipt = executor.accept(grant, "1", {"channel_id": "101", "content": "Result"},
                              idempotency_key="submission-a", work_id=work.id, revision=work.revision)
    with executor.transaction() as db:
        db.execute("UPDATE yui_effects SET state='submitted', lease_until=0 WHERE id=?", (receipt.id,))

    runtime = composed.build()
    assert watch._service is runtime.schedules
    assert store.get(work.id).state == "needs_reconciliation"
    assert runtime.schedules.executor.get(receipt.id).state == "needs_reconciliation"
    assert runtime.schedules.executor.pending_work_ids() == {work.id}
    assert len(bot.views) == 1
    message_id, view = bot.views[0]
    assert message_id == 777
    assert isinstance(view, WorkQuestionView)
    assert view.children[0].custom_id == f"work-answer:{question['id']}"
    assert all(channel.sent == [] for channel in bot.channels.values())
    await runtime.close()


async def test_actual_observer_respects_owner_participants_and_recipient_audience(composed):
    from bot_commands import watch

    store = composed.store
    shared = store.create("1", "101", "Shared comparison", "shared", guild_id="10", participants=["2"])
    private = store.create("1", "101", "Private comparison", "private", guild_id="10", audience="owner")
    local = store.create("1", "101", "Channel comparison", "local", guild_id="10", audience="channel")
    runtime = composed.build()
    authorize = watch._authorize_watch
    assert authorize("1", shared.id, "102")
    assert authorize("2", shared.id, "102")
    assert not authorize("3", shared.id, "102")
    assert authorize("1", private.id, "201")
    assert not authorize("1", private.id, "101")
    assert not authorize("1", private.id, "202")
    assert not authorize("2", private.id, "202")
    assert authorize("1", local.id, "101")
    assert not authorize("1", local.id, "102")
    observation = await runtime.schedules.observers["work"](
        shared.id, {"status": "queued"}, SimpleNamespace(owner_id="2", recipient="102"),
    )
    assert observation.matched
    assert observation.value["status"] == "queued"
    assert "Shared comparison" not in observation.content
    await runtime.close()


@pytest.mark.parametrize("recipient", ["999", "103", "104"])
async def test_watch_rejects_unknown_invisible_or_other_guild_recipient(composed, recipient):
    from bot_commands import watch

    work = composed.store.create("1", "101", "Shared comparison", "shared", guild_id="10")
    runtime = composed.build()
    assert not watch._authorize_watch("1", work.id, recipient)
    await runtime.close()


async def test_bootstrap_reminders_and_watches_expire_cancel_and_do_not_duplicate(composed):
    clock = SimpleNamespace(now=1000.0)
    work = composed.store.create("1", "101", "Compare properties", "request", guild_id="10")
    runtime = composed.build()
    schedules = runtime.schedules
    schedules.clock = schedules.executor.clock = lambda: clock.now
    reminder = schedules.add_reminder("1", "101", "Review the comparison", run_at=1010)
    expired = schedules.add_reminder("1", "101", "Expired", run_at=1010, expires_at=1020)
    cancelled = schedules.add_reminder("1", "101", "Cancelled", run_at=1010)
    schedules.cancel(cancelled.id, "1")
    observation = schedules.add_watch("1", "102", observer="work", target=work.id, criteria={},
                                     expires_at=1020, interval_seconds=5, cooldown_seconds=0)
    await schedules.tick()
    assert len(composed.bot.channels[102].sent) == 1
    clock.now = 1005
    await schedules.tick()
    assert len(composed.bot.channels[102].sent) == 1
    composed.store.amend(work.id, "1", "Include commute", "amend")
    clock.now = 1010
    await schedules.tick()
    assert len(composed.bot.channels[102].sent) == 2
    assert schedules.get(reminder.id).status == "completed"
    assert schedules.get(cancelled.id).status == "cancelled"
    # Create another notification that cannot execute before its cutoff.
    missed = schedules.add_reminder("1", "101", "Missed window", run_at=1015, expires_at=1020)
    clock.now = 1021
    count = len(composed.bot.channels[101].sent)
    await schedules.tick()
    await schedules.tick()
    assert len(composed.bot.channels[101].sent) == count
    assert schedules.get(observation.id).status == "expired"
    assert schedules.get(missed.id).status == "expired"
    assert schedules.get(expired.id).status == "completed"
    await runtime.close()


async def test_actual_model_composition_hosts_search_and_persists_result(composed, monkeypatch):
    import config
    from services import deep_research
    from services.claude_api import mcp_client
    from services.work import model

    calls = []
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    read_tool = {"name": "mcp__fixture__search", "description": "Read source", "input_schema": schema}
    write_tool = {"name": "mcp__fixture__send", "description": "External send", "input_schema": schema}

    class Pool:
        def __init__(self, *, stack, available_configs):
            assert set(available_configs) == {"fixture"}
            self.handles = {}

        async def spawn_eager(self, names):
            raise AssertionError("fixture has static schemas")

        async def call_tool(self, name, args):
            assert name == read_tool["name"]
            calls.append((name, args))
            return "Verified source: commute 20 minutes."

    async def fake_model(**kwargs):
        assert kwargs["provider"] == "codex"
        assert kwargs["model"] == "test-model"
        names = {tool["name"] for tool in kwargs["tools"]}
        assert read_tool["name"] in names
        assert write_tool["name"] not in names
        assert "work_finish" in names
        evidence = await kwargs["execute"](read_tool["name"], {"query": "commute"})
        assert "20 minutes" in evidence
        await kwargs["execute"]("work_finish", {
            "complete": True, "criteria": [{"criterion": "Provide comparison", "satisfied": True,
                                            "evidence": "Verified commute source"}],
            "unknowns": [], "next_action": "",
        })
        return "The verified commute is 20 minutes."

    monkeypatch.setattr(config, "MCP_SERVERS", {"fixture": {"command": "must-not-run"}})
    monkeypatch.setattr(deep_research, "available_research_keys", lambda: ["fixture"])
    monkeypatch.setattr(mcp_client, "McpPool", Pool)
    monkeypatch.setattr(mcp_client, "STATIC_MCP_TOOL_SCHEMAS", {"fixture": []})
    monkeypatch.setattr(mcp_client, "static_mcp_tools_to_anthropic", lambda keys: [read_tool, write_tool])
    monkeypatch.setattr(model, "run_work_model", fake_model)
    work = composed.store.create("1", "101", "Compare commute", "request", guild_id="10",
                                 completion_criteria=["Provide comparison"])
    runtime = composed.build()
    task = await runtime.start(work.id, "1", composed.bot.channels[101])
    await task
    assert composed.store.get(work.id).state == "completed"
    events = composed.store.list_events(work.id)
    assert any(event["kind"] == "evidence_acquired" for event in events)
    assert any(event["kind"] == "effect_confirmed" for event in events)
    assert len(calls) == 1
    artifacts = composed.store.list_artifacts(work.id, "1")
    assert len(artifacts) == 1
    assert "20 minutes" in artifacts[0]["body"]
    assert len(composed.bot.channels[101].sent) == 1
    await runtime.close()

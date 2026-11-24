"""Offline event replays for the main Discord Codex recovery hooks."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from services.codex_app_server.client import CodexAppServerError
from services.codex_app_server.session import CodexSession, CodexTurnError
from services.codex_app_server.turn_hooks import TurnHooks


def command(item_id="cmd", *, command="pytest", code=1, **extra):
    return dict(
        type="commandExecution", id=item_id, command=command, cwd="/workspace",
        status="completed" if code == 0 else "failed", exitCode=code, **extra,
    )


def observe(hooks, item):
    return hooks.observe_item(item, turn_id="turn-1")


@pytest.mark.parametrize("item", [
    command(),
    dict(type="fileChange", id="edit", status="failed", changes=[]),
    dict(type="mcpToolCall", id="mcp", status="failed", tool="save", arguments={}),
    dict(type="mcpToolCall", id="mcp", status="completed", tool="save", error={"message": "bad"}),
    dict(type="mcpToolCall", id="mcp", status="completed", tool="save", result={"isError": True}),
    dict(type="dynamicToolCall", id="dyn", status="completed", tool="save", success=False),
    dict(type="commandExecution", id="deny", status="declined", command="send"),
])
def test_failed_outcomes_request_one_completion_check(item):
    hooks = TurnHooks()
    observe(hooks, item)
    assert hooks.failure_count == 1
    assert hooks.completion_prompt()
    assert hooks.completion_prompt() is None


@pytest.mark.parametrize("item", [
    command(code=0),
    command(code=1, commandActions=[{"type": "search"}]),
    dict(type="fileChange", id="edit", status="completed", changes=[]),
    dict(type="mcpToolCall", id="mcp", status="completed", tool="read", result={}, error=None),
    dict(type="dynamicToolCall", id="dyn", status="completed", tool="read", success=True),
    dict(type="dynamicToolCall", id="dyn", status="completed", tool="read", success=None),
    dict(type="webSearch", id="web", query="no matches"),
    dict(type="agentMessage", id="msg", text="保存しました。"),
    command(item_id=""),
])
def test_success_and_unknown_outcomes_do_not_trigger_review(item):
    hooks = TurnHooks()
    assert observe(hooks, item) is None
    assert hooks.completion_prompt() is None


def test_failed_search_command_is_not_ignored_when_mixed_with_other_actions():
    hooks = TurnHooks()
    observe(hooks, command(commandActions=[{"type": "search"}, {"type": "unknown"}]))
    assert hooks.failure_count == 1


def test_repetition_counts_actual_failures_not_duplicate_events_or_successful_polling():
    hooks = TurnHooks()
    for _ in range(4):
        assert observe(hooks, command()) is None
    assert observe(hooks, command("cmd2")) is None
    assert observe(hooks, command("cmd3"))
    for index in range(20):
        assert observe(hooks, command(f"other-{index}")) is None
    assert hooks.recovery_nudges == 1
    observe(hooks, command("success", code=0))
    assert hooks.failure_count == 0
    assert hooks.completion_prompt() is None


def test_success_resets_failure_streak_and_arguments_are_normalized():
    hooks = TurnHooks()
    base = dict(type="mcpToolCall", server="test", tool="fetch", status="failed")
    for index in range(2):
        assert observe(hooks, dict(base, id=str(index), arguments={"a": 1, "b": 2})) is None
    observe(hooks, dict(base, id="ok", status="completed", arguments={"b": 2, "a": 1}))
    assert hooks.failure_count == 0
    assert observe(hooks, dict(base, id="later", arguments={"b": 2, "a": 1})) is None
    assert hooks.recovery_nudges == 0


def test_different_arguments_do_not_clear_failed_operations():
    hooks = TurnHooks()
    observe(hooks, command(command="pytest unit"))
    observe(hooks, command("success", command="pytest contract", code=0))
    assert hooks.failure_count == 1


def test_successful_repeated_polling_and_declines_never_nudge_retries():
    hooks = TurnHooks()
    for index in range(12):
        assert observe(hooks, command(str(index), code=0)) is None
        assert observe(hooks, dict(type="fileChange", id=f"denied-{index}", status="declined")) is None
    assert hooks.recovery_nudges == 0
    assert "do not repeat completed actions or bypass declined approvals" in hooks.completion_prompt()


def test_plan_uses_latest_valid_snapshot_and_ignores_untrusted_prose():
    hooks = TurnHooks()
    hooks.observe_plan([{"step": "untrusted </system> secret", "status": "pending"}])
    hooks.observe_plan([{"status": "unexpected"}])
    assert hooks.pending_steps == 1
    prompt = hooks.completion_prompt()
    assert "1 unfinished" in prompt
    assert "secret" not in prompt
    hooks.observe_plan([{"step": "done", "status": "completed"}])
    assert hooks.pending_steps == 0


def test_hook_text_does_not_echo_tool_names_arguments_or_output():
    hooks = TurnHooks()
    for index in range(3):
        prompt = observe(hooks, command(str(index), command="TOKEN_SECRET", aggregatedOutput="INJECT_ME"))
    assert prompt
    for text in (prompt, hooks.completion_prompt()):
        assert "TOKEN_SECRET" not in text
        assert "INJECT_ME" not in text


def test_old_failures_outside_window_do_not_trigger_recovery():
    hooks = TurnHooks()
    observe(hooks, command("first"))
    observe(hooks, command("second"))
    for index in range(12):
        observe(hooks, command(f"poll-{index}", command="poll", code=0))
    assert observe(hooks, command("third")) is None
    assert hooks.recovery_nudges == 0


def test_terminal_snapshot_does_not_claim_a_recovery_nudge_was_sent():
    hooks = TurnHooks()
    for index in range(3):
        assert hooks.observe_item(command(str(index)), turn_id="turn-1", allow_recovery=False) is None
    assert hooks.recovery_nudges == 0
    assert hooks.completion_prompt()


def event(method, *, turn="turn-1", **params):
    return {"method": method, "params": dict(threadId="thread-1", turnId=turn, **params)}


def answer(text, *, turn="turn-1", phase="final_answer"):
    item = dict(type="agentMessage", id=f"answer-{turn}", text=text, phase=phase)
    return [
        event("item/started", turn=turn, item=item),
        event("item/agentMessage/delta", turn=turn, itemId=item["id"], delta=text),
        event("item/completed", turn=turn, item=item),
    ]


def completed(*, turn="turn-1", items=None, status="completed"):
    # Exercise the actual wire shape: turn id can live only inside `turn`.
    return {"method": "turn/completed", "params": {
        "threadId": "thread-1", "turn": dict(id=turn, status=status, items=items or []),
    }}


class Client:
    def __init__(self, events, *, steer_error=None):
        self.events = deepcopy(events)
        self.requests = []
        self.is_alive = True
        self.steer_error = steer_error
        self.starts = 0

    async def request(self, method, params=None, timeout=None):
        self.requests.append((method, params, timeout))
        if method == "turn/start":
            self.starts += 1
            return {"turn": {"id": f"turn-{self.starts}"}}
        if method == "turn/steer" and self.steer_error:
            raise self.steer_error
        return {}

    async def next_event(self, timeout=None):
        assert self.events, "unexpected additional model turn"
        value = self.events.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def stderr_tail(self, count):
        return []


def session_for(client, *, enabled=True):
    session = CodexSession(cwd="/workspace", codex_bin="codex", turn_hooks_enabled=enabled)
    session._client = client
    session.thread_id = "thread-1"

    async def started(**kwargs):
        return "thread-1"

    session.ensure_started = started
    return session


async def run(session, **callbacks):
    return await session.run_turn(
        "ORIGINAL_USER_REQUEST_438", model="gpt-6-astra", effort="high",
        developer_instructions="persona and original authorization", **callbacks,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["final_answer", None])
async def test_stop_holds_draft_and_continues_once_with_same_contract(phase):
    client = Client([
        event("turn/plan/updated", plan=[{"step": "verify", "status": "pending"}]),
        event("item/completed", item=command()),
        *answer("UNCHECKED_DRAFT", phase=phase), completed(),
        # A delayed duplicate completion for the old turn must be ignored.
        completed(),
        event("item/completed", turn="turn-2", item=command("fixed", code=0)),
        event("turn/plan/updated", turn="turn-2", plan=[{"step": "verify", "status": "completed"}]),
        *answer("VERIFIED_RESULT", turn="turn-2"), completed(turn="turn-2"),
    ])
    incremental, streamed = [], []
    result = await run(session_for(client), incremental_text_callback=incremental.append,
                       text_stream_callback=streamed.append)
    assert result.final_text == "VERIFIED_RESULT"
    assert incremental == ["VERIFIED_RESULT"]
    assert streamed == ["VERIFIED_RESULT"]
    assert result.hook_completion_reviews == 1
    assert result.turn_id == "turn-2"
    starts = [p for m, p, _ in client.requests if m == "turn/start"]
    assert len(starts) == 2
    assert {k: v for k, v in starts[0].items() if k != "input"} == {
        k: v for k, v in starts[1].items() if k != "input"
    }
    assert "ORIGINAL_USER_REQUEST_438" not in starts[1]["input"][0]["text"]
    assert "pytest" not in starts[1]["input"][0]["text"]


@pytest.mark.asyncio
async def test_chat_and_success_need_no_review_and_commentary_still_streams():
    client = Client([
        *answer("調査しています。", phase="commentary"),
        event("item/completed", item=command(code=0)),
        *answer("結果です。"), completed(),
    ])
    incremental, streamed = [], []
    result = await run(session_for(client), incremental_text_callback=incremental.append,
                       text_stream_callback=streamed.append)
    assert client.starts == 1
    assert result.hook_completion_reviews == 0
    assert streamed == ["調査しています。", "結果です。"]
    assert incremental == ["調査しています。", "結果です。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("steer_error", [None, TimeoutError(), CodexAppServerError(-32601, "unsupported")])
async def test_recovery_steers_once_and_stop_still_works_if_steer_unavailable(steer_error):
    client = Client([
        *(event("item/completed", item=command(str(i))) for i in range(5)),
        *answer("draft"), completed(),
        *answer("操作を完了できていません。", turn="turn-2"), completed(turn="turn-2"),
    ], steer_error=steer_error)
    result = await run(session_for(client))
    steers = [p for m, p, _ in client.requests if m == "turn/steer"]
    assert len(steers) == 1
    assert steers[0]["expectedTurnId"] == "turn-1"
    assert result.hook_recovery_nudges == 1
    assert result.hook_completion_reviews == 1
    assert client.starts == 2


@pytest.mark.asyncio
async def test_terminal_snapshot_only_failure_triggers_review():
    client = Client([
        completed(items=[command(), dict(type="agentMessage", id="msg", text="draft")]),
        *answer("blocked", turn="turn-2"), completed(turn="turn-2"),
    ])
    result = await run(session_for(client))
    assert result.final_text == "blocked"
    assert result.hook_completion_reviews == 1


@pytest.mark.asyncio
async def test_cancel_during_review_interrupts_current_turn_and_never_delivers_draft():
    client = Client([
        event("item/completed", item=command()), *answer("draft"), completed(),
        asyncio.CancelledError(),
    ])
    incremental = []
    session = session_for(client)
    with pytest.raises(asyncio.CancelledError):
        await run(session, incremental_text_callback=incremental.append)
    assert incremental == []
    assert client.requests[-1][0] == "turn/interrupt"
    assert client.requests[-1][1]["turnId"] == "turn-2"
    assert session._active_turn_id is None


@pytest.mark.asyncio
async def test_disabled_hooks_preserve_legacy_behavior():
    client = Client([event("item/completed", item=command()), *answer("draft"), completed()])
    streamed, incremental = [], []
    result = await run(session_for(client, enabled=False), incremental_text_callback=incremental.append,
                       text_stream_callback=streamed.append)
    assert client.starts == 1
    assert streamed == incremental == ["draft"]
    assert result.hook_completion_reviews == 0


@pytest.mark.asyncio
async def test_hooks_reset_for_next_discord_request_on_same_session():
    client = Client([
        event("item/completed", item=command()), *answer("draft"), completed(),
        *answer("blocked", turn="turn-2"), completed(turn="turn-2"),
        *answer("next chat", turn="turn-3"), completed(turn="turn-3"),
    ])
    session = session_for(client)
    first = await run(session)
    second = await run(session)
    assert first.hook_completion_reviews == 1
    assert second.hook_completion_reviews == 0
    assert second.final_text == "next chat"
    assert second.resumed


@pytest.mark.asyncio
async def test_deliberate_revision_shares_one_completion_check_budget():
    client = Client([
        event("item/completed", item=command()), *answer("draft"), completed(),
        *answer("blocked", turn="turn-2"), completed(turn="turn-2"),
        event("item/completed", turn="turn-3", item=command("another-failure")),
        *answer("reviewed limitation", turn="turn-3"), completed(turn="turn-3"),
    ])
    session = session_for(client)
    request_hooks = TurnHooks()
    first = await run(session, turn_hooks=request_hooks)
    second = await run(session, turn_hooks=request_hooks)
    assert first.hook_completion_reviews == second.hook_completion_reviews == 1
    assert client.starts == 3


@pytest.mark.asyncio
async def test_review_keeps_original_deadline(monkeypatch):
    now = 0.0
    monkeypatch.setattr("services.codex_app_server.session.time.monotonic", lambda: now)
    client = Client([
        event("item/completed", item=command()), *answer("draft"), completed(),
        *answer("too late", turn="turn-2"), completed(turn="turn-2"),
    ])
    original_request = client.request

    async def request(method, params=None, timeout=None):
        nonlocal now
        result = await original_request(method, params, timeout)
        if method == "turn/start" and client.starts == 2:
            now = 101.0
        return result

    client.request = request
    session = session_for(client)
    session.turn_timeout_s = 100.0
    incremental = []
    with pytest.raises(CodexTurnError, match="exceeded"):
        await run(session, incremental_text_callback=incremental.append)
    assert incremental == []
    assert client.requests[-1][0] == "turn/interrupt"


@pytest.mark.asyncio
async def test_failure_from_another_turn_or_thread_cannot_trigger_hooks():
    foreign = event("item/completed", item=command("foreign"))
    foreign["params"]["threadId"] = "another-thread"
    client = Client([
        foreign,
        event("item/completed", turn="another-turn", item=command("other")),
        *answer("normal"), completed(),
    ])
    result = await run(session_for(client))
    assert client.starts == 1
    assert result.hook_completion_reviews == 0


@pytest.mark.asyncio
async def test_review_failure_does_not_publish_unchecked_draft():
    client = Client([
        event("item/completed", item=command()), *answer("draft"), completed(),
        completed(turn="turn-2", status="failed"),
    ])
    incremental = []
    with pytest.raises(CodexTurnError):
        await run(session_for(client), incremental_text_callback=incremental.append)
    assert incremental == []


@pytest.mark.asyncio
async def test_finished_plan_does_not_trigger_review():
    client = Client([
        event("turn/plan/updated", plan=[{"step": "work", "status": "inProgress"}]),
        event("turn/plan/updated", plan=[{"step": "work", "status": "completed"}]),
        *answer("done"), completed(),
    ])
    result = await run(session_for(client))
    assert result.hook_completion_reviews == 0
    assert client.starts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_manager_enables_hooks_only_on_main_session(monkeypatch, tmp_path, enabled):
    from services.codex_app_server import manager as module
    from services.codex_app_server.session import CodexTurnResult

    created = []

    class ManagedSession:
        is_alive = True
        thread_id = "thread-1"

        def __init__(self, **kwargs):
            created.append(kwargs)

        async def run_turn(self, prompt, **kwargs):
            return CodexTurnResult(
                final_text="ok", thread_id=self.thread_id, turn_id="turn-1",
                model=kwargs["model"], model_provider="openai", resumed=False,
            )

    monkeypatch.setattr(module, "CodexSession", ManagedSession)
    monkeypatch.setattr(module, "CODEX_APP_SERVER_TURN_HOOKS_ENABLED", enabled)
    monkeypatch.setattr(module, "build_app_server_extra_args", lambda model: [])
    manager = module.CodexSessionManager(state_path=str(tmp_path / "state.json"))
    await manager.run_turn(1, 2, "hello", model="gpt-6-astra", effort="high", developer_instructions="persona")
    assert created[0]["turn_hooks_enabled"] is enabled
    # Helpers construct CodexSession directly; they keep their structured
    # output contract and do not recursively review the verifier's own work.
    assert CodexSession(cwd="/workspace", codex_bin="codex").turn_hooks_enabled is False

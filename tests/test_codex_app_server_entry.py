from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.codex_app_server import entry
from services.codex_app_server.session import CodexTurnResult


def _result() -> CodexTurnResult:
    return CodexTurnResult(
        final_text="answer",
        thread_id="thread-1",
        turn_id="turn-1",
        model="gpt-5.6-sol",
        model_provider="openai",
        resumed=False,
    )


@pytest.mark.asyncio
async def test_new_thread_does_not_duplicate_bot_prepared_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = type("Manager", (), {})()
    manager.has_thread = lambda _channel, _user: False
    manager.has_live_session = lambda _channel, _user: False
    manager.run_turn = AsyncMock(return_value=_result())
    monkeypatch.setattr(entry, "get_manager", lambda: manager)
    monkeypatch.setattr(entry, "append_jsonl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        entry,
        "_format_shared_history",
        lambda _channel: pytest.fail("initial history bridge must not run"),
    )

    result = await entry.call_with_tools(
        "<full_bot_context>current</full_bot_context>",
        10,
        {},
        user_id=20,
        forced_model_override="gpt-5.6-sol",
    )

    assert result == ("answer", None, False)
    args = manager.run_turn.await_args
    assert args.args[:3] == (
        10,
        20,
        "<full_bot_context>current</full_bot_context>",
    )
    assert args.kwargs["resume_fallback_context"] == ""
    instructions = args.kwargs["developer_instructions"]
    assert "configured MCP server can perform the task" in instructions
    assert "Never use Bash, curl, or another CLI as an MCP proxy" in instructions


@pytest.mark.asyncio
async def test_dormant_persisted_thread_prepares_fallback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = type("Manager", (), {})()
    manager.has_thread = lambda _channel, _user: True
    manager.has_live_session = lambda _channel, _user: False
    manager.run_turn = AsyncMock(return_value=_result())
    monkeypatch.setattr(entry, "get_manager", lambda: manager)
    monkeypatch.setattr(entry, "append_jsonl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        entry,
        "_format_shared_history",
        lambda _channel: "<untrusted_history>prior</untrusted_history>",
    )

    await entry.call_with_tools(
        "lean current turn",
        10,
        {},
        user_id=20,
        forced_model_override="gpt-5.6-sol",
    )

    args = manager.run_turn.await_args
    assert args.args[2] == "lean current turn"
    assert args.kwargs["resume_fallback_context"] == (
        "<untrusted_history>prior</untrusted_history>"
    )


@pytest.mark.asyncio
async def test_active_skill_uses_developer_instructions_not_user_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = type("Manager", (), {})()
    manager.has_thread = lambda _channel, _user: False
    manager.has_live_session = lambda _channel, _user: False
    manager.run_turn = AsyncMock(return_value=_result())
    monkeypatch.setattr(entry, "get_manager", lambda: manager)
    monkeypatch.setattr(entry, "append_jsonl", lambda *_args, **_kwargs: None)

    await entry.call_with_tools(
        "raw current request",
        10,
        {},
        user_id=20,
        raw_user_text="raw current request",
        active_skill_prompt="<active_local_skill>INSHI</active_local_skill>",
    )

    call = manager.run_turn.await_args
    assert call.args[2] == "raw current request"
    instructions = call.kwargs["developer_instructions"]
    assert "<active_local_skill>INSHI</active_local_skill>" in instructions
    assert instructions.index("<active_local_skill>") < instructions.index(
        "## OpenAI Codex Discord runtime adapter"
    )

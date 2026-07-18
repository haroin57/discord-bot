"""Regression tests for cross-provider continuity invalidation."""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from services import provider_router
from services.providers.contracts import ProviderRequest, ProviderResult
from services.providers.workflow import ProviderContinuityError, ProviderWorkflow


@pytest.fixture(autouse=True)
def _isolated_session_registry(tmp_path, monkeypatch):
    import services.session as session

    monkeypatch.setattr(session, "SESSIONS_PATH", str(tmp_path / "sessions.json"))
    session.reset_session_registry_for_tests()
    yield
    session.reset_session_registry_for_tests()


def _direct_request() -> ProviderRequest:
    return ProviderRequest(
        prompt="prompt",
        channel_id=10,
        sessions={},
        user_id=20,
    )


async def test_direct_workflow_loads_v2_before_invalid_marker_preflight(
    monkeypatch,
):
    from services.codex_app_server import manager
    import services.session as session

    payload = {
        "version": 2,
        "revision": 12,
        "sessions": [
            {
                "channel_id": 10,
                "user_id": 20,
                "session_id": "existing-v2-session",
                "mcp_server_keys": ["existing-mcp"],
            },
            {
                "channel_id": 90,
                "user_id": 91,
                "session_id": "unrelated-v2-session",
                "mcp_server_keys": [],
            },
        ],
        "invalidated_providers": [
            {"provider": "codex", "channel_id": 10, "user_id": 20},
            {"provider": "claude", "channel_id": 90, "user_id": 91},
        ],
        "invalidate_all_provider_continuity": False,
        "validated_providers": [],
    }
    with open(session.SESSIONS_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    dispatcher = AsyncMock()
    workflow = ProviderWorkflow(dispatcher)
    cleanup = AsyncMock(side_effect=OSError("Codex cleanup unavailable"))
    monkeypatch.setattr(manager, "clear_codex_thread", cleanup)

    with pytest.raises(
        ProviderContinuityError,
        match="stale Codex provider continuity",
    ):
        await workflow.run(_direct_request(), "codex")

    dispatcher.dispatch.assert_not_awaited()
    cleanup.assert_awaited_once_with(10, 20)
    loaded = session.get_session(10, 20)
    assert loaded is not None
    assert loaded.session_id == "existing-v2-session"
    assert session.is_provider_continuity_invalid("codex", 10, 20) is True
    assert session.is_provider_continuity_invalid("claude", 90, 91) is True
    with open(session.SESSIONS_PATH, encoding="utf-8") as handle:
        assert json.load(handle) == payload


async def test_direct_workflow_first_use_is_shared_with_flush_and_close(
    monkeypatch,
):
    from services.codex_app_server import manager
    import services.session as session

    payload = {
        "version": 2,
        "revision": 4,
        "sessions": [
            {
                "channel_id": 10,
                "user_id": 20,
                "session_id": "preserved-session",
                "mcp_server_keys": [],
            }
        ],
        "invalidated_providers": [
            {"provider": "claude", "channel_id": 90, "user_id": 91}
        ],
        "invalidate_all_provider_continuity": False,
        "validated_providers": [],
    }
    with open(session.SESSIONS_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    load_started = threading.Event()
    release_load = threading.Event()
    load_calls = 0
    registry = session.session_registry()
    real_load = registry.repository.load

    def _blocking_load():
        nonlocal load_calls
        load_calls += 1
        load_started.set()
        assert release_load.wait(timeout=2)
        return real_load()

    monkeypatch.setattr(registry.repository, "load", _blocking_load)
    monkeypatch.setattr(manager, "clear_codex_thread", AsyncMock(return_value=True))
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = ProviderResult(
        text="claude",
        session=None,
        resumed=False,
        provider="claude",
    )
    workflow = ProviderWorkflow(dispatcher)

    first = asyncio.create_task(workflow.run(_direct_request(), "claude"))
    assert await asyncio.to_thread(load_started.wait, 2)
    second = asyncio.create_task(workflow.run(_direct_request(), "claude"))
    flush = asyncio.create_task(session.flush_session_registry())
    await asyncio.sleep(0)
    assert first.done() is False
    assert second.done() is False
    assert flush.done() is False

    release_load.set()
    first_result, second_result, _ = await asyncio.gather(first, second, flush)

    assert first_result.text == "claude"
    assert second_result.text == "claude"
    assert load_calls == 1
    assert dispatcher.dispatch.await_count == 2
    preserved = session.get_session(10, 20)
    assert preserved is not None
    assert preserved.session_id == "preserved-session"
    assert session.is_provider_continuity_invalid("claude", 90, 91) is True

    await session.close_session_registry()
    with pytest.raises(
        ProviderContinuityError,
        match="session registry is unavailable",
    ):
        await workflow.run(_direct_request(), "claude")
    assert dispatcher.dispatch.await_count == 2


async def test_direct_workflow_load_failure_is_sticky_and_fail_closed(
    monkeypatch,
):
    import services.session as session

    payload = {
        "version": 2,
        "revision": 8,
        "sessions": [],
        "invalidated_providers": [
            {"provider": "codex", "channel_id": 10, "user_id": 20}
        ],
        "invalidate_all_provider_continuity": False,
        "validated_providers": [],
    }
    with open(session.SESSIONS_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    load_calls = 0

    def _failing_load():
        nonlocal load_calls
        load_calls += 1
        raise OSError("session state unavailable")

    monkeypatch.setattr(session.session_registry().repository, "load", _failing_load)
    dispatcher = AsyncMock()
    workflow = ProviderWorkflow(dispatcher)

    results = await asyncio.gather(
        workflow.run(_direct_request(), "codex"),
        workflow.run(_direct_request(), "codex"),
        return_exceptions=True,
    )

    assert all(isinstance(result, ProviderContinuityError) for result in results)
    assert load_calls == 1
    dispatcher.dispatch.assert_not_awaited()
    with open(session.SESSIONS_PATH, encoding="utf-8") as handle:
        assert json.load(handle) == payload


async def test_failed_dormant_cleanup_is_recorded_and_blocks_stale_codex_resume(
    monkeypatch,
):
    from services import claude_api
    from services.codex_app_server import entry as codex_entry
    from services.codex_app_server import manager
    from services.session import (
        flush_session_registry,
        is_provider_continuity_invalid,
        shared_sessions,
    )

    claude_call = AsyncMock(return_value=("claude", None, False))
    codex_call = AsyncMock(return_value=("codex", None, False))
    cleanup = AsyncMock(side_effect=OSError("state fsync failed"))
    monkeypatch.setattr(claude_api, "call_with_tools", claude_call)
    monkeypatch.setattr(codex_entry, "call_with_tools", codex_call)
    monkeypatch.setattr(manager, "clear_codex_thread", cleanup)

    result = await provider_router.call_with_provider(
        "prompt",
        10,
        shared_sessions(),
        provider="claude",
        user_id=20,
    )

    assert result == ("claude", None, False)
    assert is_provider_continuity_invalid("codex", 10, 20) is True
    import services.session as session

    with open(session.SESSIONS_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert {
        "provider": "codex",
        "channel_id": 10,
        "user_id": 20,
    } in payload["invalidated_providers"]

    with pytest.raises(ProviderContinuityError):
        await provider_router.call_with_provider(
            "next",
            10,
            shared_sessions(),
            provider="codex",
            user_id=20,
        )
    codex_call.assert_not_awaited()
    await flush_session_registry()


async def test_preflight_cleanup_recovers_then_invalidates_dormant_claude(
    monkeypatch,
):
    from services.codex_app_server import entry as codex_entry
    from services.codex_app_server import manager
    from services.session import (
        SessionInfo,
        flush_session_registry,
        get_session,
        invalidate_provider_continuity,
        is_provider_continuity_invalid,
        set_session,
        shared_sessions,
    )

    cleanup = AsyncMock(return_value=True)
    codex_call = AsyncMock(return_value=("codex", None, False))
    monkeypatch.setattr(manager, "clear_codex_thread", cleanup)
    monkeypatch.setattr(codex_entry, "call_with_tools", codex_call)
    set_session(10, 20, SessionInfo("claude-session"))
    invalidate_provider_continuity("codex", 10, 20)

    result = await provider_router.call_with_provider(
        "prompt",
        10,
        shared_sessions(),
        provider="codex",
        user_id=20,
    )

    assert result == ("codex", None, False)
    cleanup.assert_awaited_once_with(10, 20)
    assert get_session(10, 20) is None
    assert is_provider_continuity_invalid("claude", 10, 20) is True
    assert is_provider_continuity_invalid("codex", 10, 20) is False
    await flush_session_registry()


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_codex_response_waits_for_durable_claude_invalidation(
    monkeypatch,
    cleanup_fails,
):
    from services.claude_api import session_store
    from services.codex_app_server import entry as codex_entry
    import services.session as session

    codex_call = AsyncMock(return_value=("codex", None, False))
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()
    cleanup_calls = []

    def _clear_session_time(channel_id, user_id):
        cleanup_calls.append((channel_id, user_id))
        if cleanup_fails:
            raise OSError("timestamp cleanup failed")

    async def _durable_flush():
        flush_started.set()
        await release_flush.wait()
        await session.session_registry().flush()

    monkeypatch.setattr(codex_entry, "call_with_tools", codex_call)
    monkeypatch.setattr(session_store, "_clear_session_time", _clear_session_time)
    monkeypatch.setattr(session, "flush_session_registry", _durable_flush)

    call_task = asyncio.create_task(
        provider_router.call_with_provider(
            "prompt",
            10,
            session.shared_sessions(),
            provider="codex",
            user_id=20,
        )
    )
    await asyncio.wait_for(flush_started.wait(), timeout=1)

    assert call_task.done() is False
    assert session.is_provider_continuity_invalid("claude", 10, 20) is True

    release_flush.set()
    assert await call_task == ("codex", None, False)
    assert cleanup_calls == [(10, 20)]

    with open(session.SESSIONS_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert {
        "provider": "claude",
        "channel_id": 10,
        "user_id": 20,
    } in payload["invalidated_providers"]


async def test_cleanup_marker_save_failure_blocks_provider_response(monkeypatch):
    from services import claude_api
    from services.codex_app_server import manager
    import services.session as session

    claude_call = AsyncMock(return_value=("claude response", None, False))
    cleanup = AsyncMock(side_effect=OSError("codex cleanup failed"))
    durable_flush = AsyncMock(side_effect=OSError("session fsync failed"))
    monkeypatch.setattr(claude_api, "call_with_tools", claude_call)
    monkeypatch.setattr(manager, "clear_codex_thread", cleanup)
    monkeypatch.setattr(session, "flush_session_registry", durable_flush)

    with pytest.raises(
        ProviderContinuityError,
        match="cleanup marker could not be persisted",
    ):
        await provider_router.call_with_provider(
            "prompt",
            10,
            session.shared_sessions(),
            provider="claude",
            user_id=20,
        )

    claude_call.assert_awaited_once()
    durable_flush.assert_awaited_once()
    assert session.is_provider_continuity_invalid("codex", 10, 20) is True


async def test_claude_dispatch_failure_durably_retires_codex(monkeypatch):
    from services import claude_api
    from services.codex_app_server import manager
    import services.session as session

    dispatch_error = RuntimeError("Claude failed after provider side effect")
    provider_state = {}

    async def _failing_claude_call(*_args, **_kwargs):
        provider_state["created"] = True
        raise dispatch_error

    cleanup = AsyncMock(side_effect=OSError("Codex cleanup unavailable"))
    monkeypatch.setattr(claude_api, "call_with_tools", _failing_claude_call)
    monkeypatch.setattr(manager, "clear_codex_thread", cleanup)

    with pytest.raises(RuntimeError) as caught:
        await provider_router.call_with_provider(
            "prompt",
            10,
            session.shared_sessions(),
            provider="claude",
            user_id=20,
        )

    assert caught.value is dispatch_error
    traceback_names = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        traceback_names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "_failing_claude_call" in traceback_names
    assert provider_state == {"created": True}
    cleanup.assert_awaited_once_with(10, 20)
    assert session.is_provider_continuity_invalid("codex", 10, 20) is True
    with open(session.SESSIONS_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert {
        "provider": "codex",
        "channel_id": 10,
        "user_id": 20,
    } in payload["invalidated_providers"]


async def test_claude_dispatch_cancel_retires_codex_through_repeated_cancel(
    monkeypatch,
):
    from services import claude_api
    from services.codex_app_server import manager
    from services.providers import workflow as workflow_module
    import services.session as session

    dispatch_started = asyncio.Event()
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()
    flush_finished = asyncio.Event()
    real_flush = session.flush_session_registry

    async def _cancelled_claude_call(*_args, **_kwargs):
        dispatch_started.set()
        await asyncio.Future()

    async def _blocking_flush():
        flush_started.set()
        await release_flush.wait()
        await real_flush()
        flush_finished.set()

    monkeypatch.setattr(claude_api, "call_with_tools", _cancelled_claude_call)
    monkeypatch.setattr(
        manager,
        "clear_codex_thread",
        AsyncMock(side_effect=OSError("Codex cleanup unavailable")),
    )
    monkeypatch.setattr(session, "flush_session_registry", _blocking_flush)

    call_task = asyncio.create_task(
        provider_router.call_with_provider(
            "prompt",
            10,
            session.shared_sessions(),
            provider="claude",
            user_id=20,
        )
    )
    await asyncio.wait_for(dispatch_started.wait(), timeout=1)
    assert call_task.cancel("first Claude cancellation") is True
    await asyncio.wait_for(flush_started.wait(), timeout=1)
    assert any(
        task.get_name().startswith("provider-continuity-cleanup:claude:")
        for task in workflow_module._continuity_cleanup_tasks
    )

    assert call_task.cancel("second Claude cancellation") is True
    await asyncio.sleep(0)
    assert call_task.done() is False
    assert call_task.cancel("third Claude cancellation") is True
    await asyncio.sleep(0)
    assert call_task.done() is False

    release_flush.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await call_task

    assert caught.value.args == ("first Claude cancellation",)
    assert flush_finished.is_set()
    assert workflow_module._continuity_cleanup_tasks == set()
    assert session.is_provider_continuity_invalid("codex", 10, 20) is True
    with open(session.SESSIONS_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert {
        "provider": "codex",
        "channel_id": 10,
        "user_id": 20,
    } in payload["invalidated_providers"]


async def test_codex_success_is_blocked_when_claude_marker_flush_fails(
    monkeypatch,
):
    from services.claude_api import session_store
    from services.codex_app_server import entry as codex_entry
    import services.session as session

    codex_call = AsyncMock(return_value=("codex response", None, False))
    durable_flush = AsyncMock(side_effect=OSError("session fsync failed"))
    monkeypatch.setattr(codex_entry, "call_with_tools", codex_call)
    monkeypatch.setattr(session_store, "_clear_session_time", lambda *_args: None)
    monkeypatch.setattr(session, "flush_session_registry", durable_flush)

    with pytest.raises(
        ProviderContinuityError,
        match="cleanup marker could not be persisted",
    ):
        await provider_router.call_with_provider(
            "prompt",
            10,
            session.shared_sessions(),
            provider="codex",
            user_id=20,
        )

    codex_call.assert_awaited_once()
    durable_flush.assert_awaited_once()
    assert session.is_provider_continuity_invalid("claude", 10, 20) is True


async def test_codex_dispatch_failure_still_durably_invalidates_claude(
    monkeypatch,
):
    from services.claude_api import session_store
    from services.codex_app_server import entry as codex_entry
    from services.session import SessionInfo
    import services.session as session

    dispatch_error = RuntimeError("codex failed after creating thread state")
    codex_state = {}

    async def _failing_codex_call(*_args, **_kwargs):
        codex_state["created"] = True
        raise dispatch_error

    monkeypatch.setattr(codex_entry, "call_with_tools", _failing_codex_call)
    monkeypatch.setattr(session_store, "_clear_session_time", lambda *_args: None)
    session.set_session(10, 20, SessionInfo("stale-claude-session"))

    with pytest.raises(RuntimeError) as caught:
        await provider_router.call_with_provider(
            "prompt",
            10,
            session.shared_sessions(),
            provider="codex",
            user_id=20,
        )

    assert caught.value is dispatch_error
    traceback_names = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        traceback_names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "_failing_codex_call" in traceback_names
    assert codex_state == {"created": True}
    assert session.get_session(10, 20) is None
    assert session.is_provider_continuity_invalid("claude", 10, 20) is True

    with open(session.SESSIONS_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert {
        "provider": "claude",
        "channel_id": 10,
        "user_id": 20,
    } in payload["invalidated_providers"]


async def test_codex_dispatch_cancel_waits_through_repeated_cleanup_cancel(
    monkeypatch,
):
    from services.claude_api import session_store
    from services.codex_app_server import entry as codex_entry
    from services.session import SessionInfo
    import services.session as session

    dispatch_started = asyncio.Event()
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()
    flush_finished = asyncio.Event()
    real_flush = session.flush_session_registry

    async def _cancelled_codex_call(*_args, **_kwargs):
        dispatch_started.set()
        await asyncio.Future()

    async def _blocking_flush():
        flush_started.set()
        await release_flush.wait()
        await real_flush()
        flush_finished.set()

    monkeypatch.setattr(codex_entry, "call_with_tools", _cancelled_codex_call)
    monkeypatch.setattr(session_store, "_clear_session_time", lambda *_args: None)
    monkeypatch.setattr(session, "flush_session_registry", _blocking_flush)
    session.set_session(10, 20, SessionInfo("stale-claude-session"))

    call_task = asyncio.create_task(
        provider_router.call_with_provider(
            "prompt",
            10,
            session.shared_sessions(),
            provider="codex",
            user_id=20,
        )
    )
    await asyncio.wait_for(dispatch_started.wait(), timeout=1)
    assert call_task.cancel("first cancellation") is True
    await asyncio.wait_for(flush_started.wait(), timeout=1)

    assert call_task.cancel("second cancellation") is True
    await asyncio.sleep(0)
    assert call_task.done() is False
    assert call_task.cancel("third cancellation") is True
    await asyncio.sleep(0)
    assert call_task.done() is False

    release_flush.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await call_task

    assert caught.value.args == ("first cancellation",)
    assert flush_finished.is_set()
    assert session.get_session(10, 20) is None
    assert session.is_provider_continuity_invalid("claude", 10, 20) is True


async def test_dispatch_error_then_cleanup_cancel_prefers_first_cancel(
    monkeypatch,
):
    from services.claude_api import session_store
    from services.session import SessionInfo
    import services.session as session

    dispatch_error = RuntimeError("dispatch failed before cleanup")
    dispatcher = AsyncMock()
    dispatcher.dispatch.side_effect = dispatch_error
    workflow = ProviderWorkflow(dispatcher)
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()
    flush_finished = asyncio.Event()
    real_flush = session.flush_session_registry

    async def _blocking_flush():
        flush_started.set()
        await release_flush.wait()
        await real_flush()
        flush_finished.set()

    monkeypatch.setattr(session_store, "_clear_session_time", lambda *_args: None)
    monkeypatch.setattr(session, "flush_session_registry", _blocking_flush)
    session.set_session(10, 20, SessionInfo("stale-claude-session"))

    call_task = asyncio.create_task(workflow.run(_direct_request(), "codex"))
    await asyncio.wait_for(flush_started.wait(), timeout=1)
    assert call_task.cancel("first cleanup cancellation") is True
    await asyncio.sleep(0)
    assert call_task.done() is False
    assert call_task.cancel("second cleanup cancellation") is True
    await asyncio.sleep(0)
    assert call_task.done() is False

    release_flush.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await call_task

    assert caught.value.args == ("first cleanup cancellation",)
    assert flush_finished.is_set()
    assert session.get_session(10, 20) is None
    assert session.is_provider_continuity_invalid("claude", 10, 20) is True
    with open(session.SESSIONS_PATH, encoding="utf-8") as handle:
        persisted = json.load(handle)
    assert {
        "provider": "claude",
        "channel_id": 10,
        "user_id": 20,
    } in persisted["invalidated_providers"]
    await asyncio.sleep(0)
    pending_cleanup = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("provider-continuity-cleanup:")
    ]
    assert pending_cleanup == []


async def test_codex_dispatch_and_flush_failure_fails_closed_with_dispatch_cause(
    monkeypatch,
):
    from services.claude_api import session_store
    from services.codex_app_server import entry as codex_entry
    import services.session as session

    dispatch_error = RuntimeError("codex state creation failed")
    durable_flush = AsyncMock(side_effect=OSError("session fsync failed"))
    monkeypatch.setattr(
        codex_entry,
        "call_with_tools",
        AsyncMock(side_effect=dispatch_error),
    )
    monkeypatch.setattr(session_store, "_clear_session_time", lambda *_args: None)
    monkeypatch.setattr(session, "flush_session_registry", durable_flush)

    with pytest.raises(
        ProviderContinuityError,
        match="cleanup marker could not be persisted",
    ) as caught:
        await provider_router.call_with_provider(
            "prompt",
            10,
            session.shared_sessions(),
            provider="codex",
            user_id=20,
        )

    assert caught.value.__cause__ is dispatch_error
    assert any(
        "session fsync failed" in note
        for note in getattr(caught.value, "__notes__", ())
    )
    durable_flush.assert_awaited_once()
    assert session.is_provider_continuity_invalid("claude", 10, 20) is True

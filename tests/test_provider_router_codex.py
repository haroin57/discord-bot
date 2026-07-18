from __future__ import annotations

import inspect
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from services import provider_router


@pytest.fixture(autouse=True)
def _isolated_session_registry(tmp_path, monkeypatch):
    import services.session as session

    monkeypatch.setattr(session, "SESSIONS_PATH", str(tmp_path / "sessions.json"))
    session.reset_session_registry_for_tests()
    yield
    session.reset_session_registry_for_tests()


@pytest.mark.parametrize(
    ("explicit", "model", "restricted", "default", "expected"),
    [
        ("codex", None, True, "codex", "claude"),
        ("claude", "gpt-5.6-sol", False, "codex", "claude"),
        ("codex", "claude-opus-4-8", False, "claude", "codex"),
        (None, "gpt-5.6-sol", False, "claude", "codex"),
        (None, "o3", False, "claude", "codex"),
        ("invalid", "claude-opus-4-8", False, "codex", "codex"),
        (None, None, False, "invalid", "claude"),
    ],
)
def test_resolve_provider_enforces_gate_and_selection_precedence(
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    model: str | None,
    restricted: bool,
    default: str,
    expected: str,
) -> None:
    monkeypatch.setattr(provider_router, "DEFAULT_LLM_PROVIDER", default)

    assert (
        provider_router.resolve_provider(
            explicit=explicit,
            model=model,
            restricted=restricted,
        )
        == expected
    )


def test_codex_model_detection_does_not_capture_claude_models() -> None:
    assert provider_router.is_codex_model("gpt-5.6-sol") is True
    assert provider_router.is_codex_model(" O4-MINI ") is True
    assert provider_router.is_codex_model("claude-opus-4-8") is False
    assert provider_router.is_codex_model(None) is False
    assert provider_router.provider_label("codex") == "Codex"
    assert provider_router.provider_label("claude") == "Claude"


def test_legacy_shim_field_order_matches_provider_entry_signatures() -> None:
    from services import claude_api
    from services.codex_app_server import entry as codex_entry

    expected = provider_router._LEGACY_CALL_FIELDS
    assert tuple(inspect.signature(claude_api.call_with_tools).parameters) == expected
    assert tuple(inspect.signature(codex_entry.call_with_tools).parameters) == expected


def test_codex_resume_hint_rejects_invalid_stale_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import session
    from services.codex_app_server import manager

    invalid = Mock(return_value=True)
    stale_manager = Mock()
    stale_manager.has_thread.return_value = True
    monkeypatch.setattr(session, "is_provider_continuity_invalid", invalid)
    monkeypatch.setattr(manager, "get_manager", lambda: stale_manager)

    assert provider_router.provider_resume_hint("codex", 10, 20) is False
    invalid.assert_called_once_with("codex", 10, 20)
    stale_manager.has_thread.assert_not_called()


def test_codex_resume_hint_uses_live_thread_when_continuity_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import session
    from services.codex_app_server import manager

    live_manager = Mock()
    live_manager.has_thread.return_value = True
    monkeypatch.setattr(
        session,
        "is_provider_continuity_invalid",
        lambda *_args: False,
    )
    monkeypatch.setattr(manager, "get_manager", lambda: live_manager)

    assert provider_router.provider_resume_hint("codex", 10, 20) is True
    live_manager.has_thread.assert_called_once_with(10, 20)
    assert provider_router.provider_resume_hint("claude", 10, 20) is None


@pytest.mark.asyncio
async def test_call_with_provider_dispatches_to_codex_or_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import claude_api
    from services.codex_app_server import entry as codex_entry

    codex_call = AsyncMock(return_value=("codex-result", None, False))
    claude_call = AsyncMock(return_value=("claude-result", None, False))
    monkeypatch.setattr(codex_entry, "call_with_tools", codex_call)
    monkeypatch.setattr(claude_api, "call_with_tools", claude_call)

    codex_result = await provider_router.call_with_provider(
        "prompt",
        10,
        object(),
        provider="codex",
        user_id=20,
    )
    claude_result = await provider_router.call_with_provider(
        "prompt",
        10,
        object(),
        provider="claude",
        user_id=20,
    )

    assert codex_result == ("codex-result", None, False)
    assert claude_result == ("claude-result", None, False)
    codex_call.assert_awaited_once_with("prompt", 10, ANY, user_id=20)
    claude_call.assert_awaited_once_with("prompt", 10, ANY, user_id=20)


@pytest.mark.asyncio
async def test_call_with_provider_preserves_legacy_positional_callback_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import claude_api
    from services.codex_app_server import manager

    claude_call = AsyncMock(return_value=("answer", "session", True))
    monkeypatch.setattr(claude_api, "call_with_tools", claude_call)
    monkeypatch.setattr(manager, "clear_codex_thread", AsyncMock())
    callback_names = [
        "status_callback",
        "incremental_text_callback",
        "text_stream_callback",
        "tool_invocation_callback",
        "scratch_stream_callback",
        "scratch_finalize_callback",
        "thinking_stream_callback",
        "thinking_finalize_callback",
        "verify_stream_callback",
        "verify_finalize_callback",
    ]
    callbacks = [object() for _name in callback_names]
    progress = {"tokens": 0}
    sessions = object()

    result = await provider_router.call_with_provider(
        "prompt",
        10,
        sessions,
        *callbacks,
        progress,
        provider="claude",
    )

    assert result == ("answer", "session", True)
    assert claude_call.await_args.args == ("prompt", 10, sessions)
    assert list(claude_call.await_args.kwargs) == [*callback_names, "progress"]
    assert list(claude_call.await_args.kwargs.values()) == [*callbacks, progress]


@pytest.mark.asyncio
async def test_call_with_provider_rejects_duplicate_legacy_argument() -> None:
    with pytest.raises(TypeError, match="multiple values.*status_callback"):
        await provider_router.call_with_provider(
            "prompt",
            10,
            object(),
            object(),
            provider="claude",
            status_callback=object(),
        )


@pytest.mark.asyncio
async def test_call_with_provider_drops_incompatible_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.codex_app_server import entry as codex_entry

    codex_call = AsyncMock(return_value=("ok", None, False))
    monkeypatch.setattr(codex_entry, "call_with_tools", codex_call)

    await provider_router.call_with_provider(
        "prompt",
        10,
        object(),
        provider="codex",
        forced_model_override="claude-opus-4-8",
    )

    codex_call.assert_awaited_once_with(
        "prompt",
        10,
        ANY,
        forced_model_override=None,
    )


@pytest.mark.asyncio
async def test_call_with_provider_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unsupported provider"):
        await provider_router.call_with_provider(
            "prompt",
            10,
            object(),
            provider="unknown",
        )


@pytest.mark.asyncio
async def test_dormant_provider_cleanup_failure_does_not_drop_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import claude_api
    from services.codex_app_server import manager

    claude_call = AsyncMock(return_value=("completed", None, False))
    cleanup = AsyncMock(side_effect=OSError("state fsync failed"))
    monkeypatch.setattr(claude_api, "call_with_tools", claude_call)
    monkeypatch.setattr(manager, "clear_codex_thread", cleanup)

    result = await provider_router.call_with_provider(
        "prompt",
        10,
        object(),
        provider="claude",
        user_id=20,
    )

    assert result == ("completed", None, False)
    cleanup.assert_awaited_once_with(10, 20)

from __future__ import annotations

from unittest.mock import ANY, AsyncMock

import pytest

from services import provider_router


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

    assert provider_router.resolve_provider(
        explicit=explicit,
        model=model,
        restricted=restricted,
    ) == expected


def test_codex_model_detection_does_not_capture_claude_models() -> None:
    assert provider_router.is_codex_model("gpt-5.6-sol") is True
    assert provider_router.is_codex_model(" O4-MINI ") is True
    assert provider_router.is_codex_model("claude-opus-4-8") is False
    assert provider_router.is_codex_model(None) is False
    assert provider_router.provider_label("codex") == "Codex"
    assert provider_router.provider_label("claude") == "Claude"


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
            "prompt", 10, object(), provider="unknown",
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

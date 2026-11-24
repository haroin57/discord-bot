from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from services import provider_router
from services.providers.contracts import (
    ProviderCapability,
    ProviderRequest,
    ProviderResult,
    TurnCallbacks,
)


async def _text_callback(_text: str) -> None:
    return None


async def _finalize_callback() -> None:
    return None


async def _tool_callback(_name: str, _args: dict) -> None:
    return None


async def _scratch_callback(_section: str, _content: str, _op: str) -> None:
    return None


def _all_callbacks() -> TurnCallbacks:
    return TurnCallbacks(
        status_callback=_text_callback,
        incremental_text_callback=_text_callback,
        text_stream_callback=_text_callback,
        tool_invocation_callback=_tool_callback,
        scratch_stream_callback=_scratch_callback,
        scratch_finalize_callback=_finalize_callback,
        thinking_stream_callback=_text_callback,
        thinking_finalize_callback=_finalize_callback,
        verify_stream_callback=_text_callback,
        verify_finalize_callback=_finalize_callback,
        progress={"tokens": 0},
    )


def test_turn_callbacks_mask_fields_not_advertised_by_provider() -> None:
    callbacks = _all_callbacks()
    capabilities = frozenset(
        {
            ProviderCapability.STATUS,
            ProviderCapability.THINKING_STREAM,
        }
    )

    masked = callbacks.for_capabilities(capabilities)

    assert masked["status_callback"] is _text_callback
    assert masked["thinking_stream_callback"] is _text_callback
    assert masked["scratch_stream_callback"] is None
    assert masked["verify_stream_callback"] is None
    assert masked["progress"] is None


@pytest.mark.asyncio
async def test_codex_dispatch_forwards_exactly_advertised_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.codex_app_server import entry as codex_entry

    captured: dict = {}

    async def fake_call(prompt, channel_id, sessions, **kwargs):
        captured.update(kwargs)
        return "codex", None, True

    monkeypatch.setattr(codex_entry, "call_with_tools", fake_call)
    monkeypatch.setattr("services.session.clear_session", lambda *_args: None)
    request = ProviderRequest(
        prompt="prompt",
        channel_id=10,
        sessions={},
        callbacks=_all_callbacks(),
        user_id=20,
    )

    result = await provider_router.call_provider(request, provider="codex")
    advertised = provider_router.provider_capabilities("codex")

    assert isinstance(result, ProviderResult)
    assert result.as_legacy_tuple() == ("codex", None, True)
    assert captured["status_callback"] is _text_callback
    assert captured["thinking_stream_callback"] is _text_callback
    assert captured["scratch_stream_callback"] is _scratch_callback
    assert captured["scratch_finalize_callback"] is _finalize_callback
    assert captured["verify_stream_callback"] is _text_callback
    assert captured["verify_finalize_callback"] is _finalize_callback
    assert ProviderCapability.STATUS in advertised
    assert ProviderCapability.SCRATCH_STREAM in advertised
    assert ProviderCapability.VERIFY_STREAM in advertised


@pytest.mark.asyncio
async def test_claude_dispatch_forwards_all_advertised_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import claude_api
    from services.codex_app_server import manager

    call = AsyncMock(return_value=("claude", object(), False))
    cleanup = AsyncMock()
    monkeypatch.setattr(claude_api, "call_with_tools", call)
    monkeypatch.setattr(manager, "clear_codex_thread", cleanup)
    request = ProviderRequest(
        prompt="prompt",
        channel_id=10,
        sessions={},
        callbacks=_all_callbacks(),
        user_id=20,
    )

    result = await provider_router.call_provider(request, provider="claude")
    kwargs = call.await_args.kwargs

    assert result.provider == "claude"
    assert kwargs["scratch_stream_callback"] is _scratch_callback
    assert kwargs["verify_stream_callback"] is _text_callback
    assert kwargs["progress"] == {"tokens": 0}
    assert provider_router.provider_capabilities("claude") == frozenset(
        ProviderCapability,
    )
    cleanup.assert_awaited_once_with(10, 20)


def test_provider_package_dependency_direction() -> None:
    root = Path(__file__).parents[1] / "services" / "providers"
    levels = {
        "contracts": 0,
        "policy": 1,
        "transport": 1,
        "execution": 2,
        "workflow": 3,
        "composition": 4,
    }
    for module, level in levels.items():
        tree = ast.parse((root / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported = node.module or ""
            prefix = "services.providers."
            if not imported.startswith(prefix):
                continue
            dependency = imported.removeprefix(prefix).split(".", 1)[0]
            assert levels[dependency] < level, (
                f"{module} must not depend on same/higher layer {dependency}"
            )

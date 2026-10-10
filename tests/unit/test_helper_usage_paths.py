from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.audit_token_usage import analyze


def telemetry(caplog):
    return [record.getMessage() for record in caplog.records if record.name == "services.token_usage"]


@pytest.fixture
def backend(monkeypatch):
    for name in ("llm_mem0", "llm_mem0.auth", "llm_mem0.auth.base"):
        module = ModuleType(name)
        module.__path__ = []
        if name.endswith(".base"):
            module.AuthBackend = object
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[2] / "services/mem0/auth_backend.py"
    spec = importlib.util.spec_from_file_location("_helper_usage_backend", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    spec.loader.exec_module(module)
    return module.BotOAuthBackend()


def compatible_client(response=None, error=None):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=response, side_effect=error),
    )))


async def test_compatible_backend_logs_actual_requested_fallback_model_and_returns_same_text(backend, monkeypatch, caplog):
    from services.mem0 import client

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"queries":["answer"]}'), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15), id="compatible-response",
    )
    provider = compatible_client(response)
    monkeypatch.setattr(client, "get_anthropic_client", AsyncMock(return_value=provider))
    monkeypatch.setattr(client, "_DEEPSEEK_V4_FLASH", "actual-fallback-model")
    with caplog.at_level(logging.INFO):
        result = await backend.complete(system="private-system", user_message="private-user", model="requested-sonnet", max_tokens=55)
    assert result == response.choices[0].message.content
    provider.chat.completions.create.assert_awaited_once_with(
        model="actual-fallback-model", max_tokens=55,
        messages=[{"role": "system", "content": "private-system"}, {"role": "user", "content": "private-user"}],
    )
    messages = telemetry(caplog)
    assert len(messages) == 1
    assert "private" not in messages[0]
    report = analyze(messages)
    assert report["compatible_helpers_by_model"]["actual-fallback-model"]["known_totals"]["prompt_tokens"] == 10
    assert report["measured_helpers_by_model"] == {}


@pytest.mark.parametrize("cancel", [False, True])
async def test_compatible_backend_failure_keeps_fail_open_or_cancellation_contract(backend, monkeypatch, caplog, cancel):
    from services.mem0 import client

    error = asyncio.CancelledError("private-error") if cancel else TimeoutError("private-error")
    provider = compatible_client(error=error)
    monkeypatch.setattr(client, "get_anthropic_client", AsyncMock(return_value=provider))
    monkeypatch.setattr(client, "_DEEPSEEK_V4_FLASH", "actual-fallback-model")
    with caplog.at_level(logging.INFO):
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await backend.complete(system="task", user_message="user", model="requested", max_tokens=55)
        else:
            assert await backend.complete(system="task", user_message="user", model="requested", max_tokens=55) == ""
    messages = telemetry(caplog)
    assert len(messages) == 1
    assert "private-error" not in messages[0]
    report = analyze(messages)
    assert report["helper_failure_observations"] == {"openai_compatible": {"cancelled" if cancel else "failed": 1}}
    assert report["compatible_helpers_by_model"] == {}


async def test_missing_provider_is_observed_without_a_model_request(backend, monkeypatch, caplog):
    from services.mem0 import client

    selector = AsyncMock(return_value=None)
    monkeypatch.setattr(client, "get_anthropic_client", selector)
    with caplog.at_level(logging.INFO):
        assert await backend.complete(system="task", user_message="user", model="requested", max_tokens=55) == ""
    selector.assert_awaited_once()
    assert analyze(telemetry(caplog))["helper_failure_observations"] == {"client_selection": {"unavailable": 1}}


@pytest.mark.parametrize("cancel", [False, True])
async def test_cli_failure_preserves_exception_and_records_unknown_usage(caplog, cancel):
    from services.mem0.cli_compat import cli_messages_create

    error = asyncio.CancelledError("private-error") if cancel else TimeoutError("private-error")
    provider = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=error)))
    with caplog.at_level(logging.INFO):
        with pytest.raises(type(error)) as captured:
            await cli_messages_create(provider, model="sonnet", max_tokens=55, user_message="private-user", task_prompt="private-system")
    assert captured.value is error
    provider.messages.create.assert_awaited_once()
    messages = telemetry(caplog)
    assert len(messages) == 1
    assert "private" not in messages[0]
    assert analyze(messages)["helper_failure_observations"] == {"anthropic_cli": {"cancelled" if cancel else "failed": 1}}


async def test_cli_success_returns_original_response_and_logs_once(caplog):
    from services.mem0.cli_compat import cli_messages_create

    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="answer")],
                               usage=dict(input_tokens=4, cache_read_input_tokens=5, cache_creation_input_tokens=6, output_tokens=7),
                               stop_reason="end_turn", id="cli-response")
    provider = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    with caplog.at_level(logging.INFO):
        result = await cli_messages_create(provider, model="sonnet", max_tokens=55, user_message="user", task_prompt="task")
    assert result is response
    assert len(telemetry(caplog)) == 1
    assert analyze(telemetry(caplog))["measured_helpers_by_model"]["sonnet"]["context_tokens"] == 15


async def test_backend_does_not_double_log_cli_failure(backend, monkeypatch, caplog):
    from services.mem0 import client

    provider = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=TimeoutError("synthetic"))))
    monkeypatch.setattr(client, "get_anthropic_client", AsyncMock(return_value=provider))
    with caplog.at_level(logging.INFO):
        assert await backend.complete(system="task", user_message="user", model="sonnet", max_tokens=55) == ""
    messages = telemetry(caplog)
    assert len(messages) == 1
    assert analyze(messages)["helper_failure_observations"] == {"anthropic_cli": {"failed": 1}}

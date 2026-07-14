from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.codex_app_server.session import (
    CodexAuthError,
    CodexProtocolError,
    CodexSession,
)
from services.codex_app_server.client import CodexAppServerError


class _ScriptedClient:
    def __init__(
        self,
        *,
        account: dict[str, Any] | None = None,
        thread: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.account = account or {"account": {"type": "chatgpt"}}
        self.thread = thread or {
            "thread": {"id": "thread-1"},
            "model": "gpt-5.6-sol",
            "modelProvider": "openai",
        }
        self.events = list(events or [])
        self.requests: list[tuple[str, Any, float | None]] = []
        self.initializations: list[dict[str, Any]] = []
        self.responses: list[tuple[Any, Any]] = []
        self.response_errors: list[tuple[Any, int, str]] = []
        self.is_alive = False
        self.closed = False

    async def start(self) -> None:
        self.is_alive = True

    async def initialize(self, **kwargs: Any) -> None:
        self.initializations.append(kwargs)

    async def request(
        self,
        method: str,
        params: Any = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.requests.append((method, params, timeout))
        if method == "account/read":
            return self.account
        if method in {"thread/start", "thread/resume"}:
            return self.thread
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(f"unexpected method: {method}")

    async def next_event(self, timeout: float | None = None) -> Any:
        del timeout
        if self.events:
            return self.events.pop(0)
        return None

    async def respond(self, request_id: Any, result: Any) -> None:
        self.responses.append((request_id, result))

    async def respond_error(
        self,
        request_id: Any,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        del data
        self.response_errors.append((request_id, code, message))

    def stderr_tail(self, count: int | None = None) -> list[str]:
        del count
        return []

    async def close(self) -> None:
        self.closed = True
        self.is_alive = False


def _session_for(client: _ScriptedClient) -> CodexSession:
    factory: Callable[..., _ScriptedClient] = lambda **_kwargs: client
    return CodexSession(
        cwd="/workspace",
        codex_bin="/opt/codex",
        client_factory=factory,
    )


@pytest.mark.asyncio
async def test_ensure_started_requires_chatgpt_oauth_account() -> None:
    client = _ScriptedClient(account={"account": {"type": "apiKey"}})
    session = _session_for(client)

    with pytest.raises(CodexAuthError, match="ChatGPT OAuth"):
        await session.ensure_started(
            model="gpt-5.6-sol",
            developer_instructions="system contract",
        )

    assert [method for method, _params, _timeout in client.requests] == [
        "account/read"
    ]
    assert client.initializations == [
        {
            "client_name": "kanae_discord_bot",
            "client_title": "Kanae Discord Bot",
            "client_version": "1.0",
            "capabilities": {},
        }
    ]
    await session.close()


@pytest.mark.asyncio
async def test_bypass_policy_reaches_client_and_thread_contract() -> None:
    client = _ScriptedClient(thread={
        "thread": {"id": "thread-1"},
        "model": "gpt-5.6-sol",
        "modelProvider": "openai",
        "sandbox": {"type": "dangerFullAccess"},
        "approvalPolicy": "never",
    })
    client_kwargs: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _ScriptedClient:
        client_kwargs.update(kwargs)
        return client

    session = CodexSession(
        cwd="/workspace",
        codex_bin="/opt/codex",
        global_args=["--dangerously-bypass-approvals-and-sandbox"],
        sandbox="danger-full-access",
        approval_policy="never",
        client_factory=factory,
    )

    await session.ensure_started(
        model="gpt-5.6-sol",
        developer_instructions="system contract",
    )

    assert client_kwargs["global_args"] == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]
    thread_request = next(
        params
        for method, params, _timeout in client.requests
        if method == "thread/start"
    )
    assert thread_request["sandbox"] == "danger-full-access"
    assert thread_request["approvalPolicy"] == "never"
    assert session.resolved_sandbox == "danger-full-access"
    assert session.resolved_approval_policy == "never"
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread_result", "message"),
    [
        (
            {
                "thread": {"id": "thread-1"},
                "model": "gpt-5.6-terra",
                "modelProvider": "openai",
            },
            "model mismatch",
        ),
        (
            {
                "thread": {"id": "thread-1"},
                "model": "gpt-5.6-sol",
                "modelProvider": "third-party",
            },
            "provider mismatch",
        ),
        (
            {
                "thread": {"id": "thread-1"},
                "model": "gpt-5.6-sol",
                "modelProvider": "openai",
                "sandbox": {"type": "dangerFullAccess"},
            },
            "sandbox mismatch",
        ),
        (
            {
                "thread": {"id": "thread-1"},
                "model": "gpt-5.6-sol",
                "modelProvider": "openai",
                "approvalPolicy": "never",
            },
            "approval policy mismatch",
        ),
    ],
)
async def test_ensure_started_rejects_model_or_provider_substitution(
    thread_result: dict[str, Any],
    message: str,
) -> None:
    client = _ScriptedClient(thread=thread_result)
    session = _session_for(client)

    with pytest.raises(CodexProtocolError, match=message):
        await session.ensure_started(
            model="gpt-5.6-sol",
            developer_instructions="system contract",
        )

    thread_request = next(
        params
        for method, params, _timeout in client.requests
        if method == "thread/start"
    )
    assert thread_request == {
        "model": "gpt-5.6-sol",
        "cwd": "/workspace",
        "approvalPolicy": "untrusted",
        "sandbox": "workspace-write",
        "developerInstructions": "system contract",
        "ephemeral": False,
    }
    await session.close()


@pytest.mark.asyncio
async def test_run_turn_translates_stream_tool_usage_and_approval_events() -> None:
    client = _ScriptedClient(
        events=[
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "tokenUsage": {
                        "last": {"inputTokens": 11, "outputTokens": 7},
                        "total": {"inputTokens": 20, "outputTokens": 9},
                        "modelContextWindow": 258400,
                    },
                },
            },
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "item-command",
                        "type": "commandExecution",
                        "command": "printf test",
                        "cwd": "/workspace",
                    },
                },
            },
            {
                "id": 41,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "command": "printf approved",
                    "cwd": "/workspace",
                    "reason": "test approval",
                },
            },
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-message",
                    "delta": "partial",
                },
            },
            {
                "method": "item/reasoning/summaryTextDelta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "delta": "reasoning summary",
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "reason", "type": "reasoning"},
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "item-message",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "DISCORD_CODEX_UNIT_OK",
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "turn": {"status": "completed", "items": []},
                },
            },
        ]
    )
    session = _session_for(client)
    session._request_approval = AsyncMock(return_value=True)
    status: list[str] = []
    text_stream: list[str] = []
    incremental: list[str] = []
    thinking: list[str] = []
    thinking_finalized: list[bool] = []
    tools: list[tuple[str, dict[str, Any]]] = []
    progress: dict[str, Any] = {}

    result = await session.run_turn(
        "reply with the marker",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="system contract",
        status_callback=status.append,
        text_stream_callback=text_stream.append,
        incremental_text_callback=incremental.append,
        thinking_stream_callback=thinking.append,
        thinking_finalize_callback=lambda: thinking_finalized.append(True),
        tool_invocation_callback=lambda name, arguments: tools.append(
            (name, arguments)
        ),
        progress=progress,
    )

    turn_request = next(
        params
        for method, params, _timeout in client.requests
        if method == "turn/start"
    )
    assert turn_request == {
        "threadId": "thread-1",
        "input": [{"type": "text", "text": "reply with the marker"}],
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "cwd": "/workspace",
        "approvalPolicy": "untrusted",
    }
    assert result.final_text == "DISCORD_CODEX_UNIT_OK"
    assert result.model == "gpt-5.6-sol"
    assert result.model_provider == "openai"
    assert result.token_usage_last == {"inputTokens": 11, "outputTokens": 7}
    assert result.token_usage_total == {"inputTokens": 20, "outputTokens": 9}
    assert result.model_context_window == 258400
    assert status == ["Codex gpt-5.6-sol へ送信中"]
    assert tools == [
        ("Bash", {"command": "printf test", "cwd": "/workspace"})
    ]
    assert client.responses == [(41, {"decision": "accept"})]
    assert text_stream == ["partial"]
    assert incremental == ["DISCORD_CODEX_UNIT_OK"]
    assert thinking == ["reasoning summary"]
    assert thinking_finalized == [True]
    assert progress == {"tokens": 7}
    await session.close()


@pytest.mark.asyncio
async def test_run_turn_rejects_runtime_model_reroute() -> None:
    client = _ScriptedClient(
        events=[
            {
                "method": "model/rerouted",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "toModel": "gpt-5.6-terra",
                },
            }
        ]
    )
    session = _session_for(client)

    with pytest.raises(CodexProtocolError, match="rerouted"):
        await session.run_turn(
            "do not reroute",
            model="gpt-5.6-sol",
            effort="xhigh",
            developer_instructions="system contract",
        )

    await session.close()


@pytest.mark.asyncio
async def test_run_turn_ignores_raw_reasoning_and_waits_through_retry() -> None:
    client = _ScriptedClient(
        events=[
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": True,
                    "error": {"message": "temporary transport failure"},
                },
            },
            {
                "method": "item/reasoning/textDelta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "delta": "private chain of thought",
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "answer",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "retry recovered",
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "turn": {"status": "completed", "items": []},
                },
            },
        ]
    )
    session = _session_for(client)
    status: list[str] = []
    thinking: list[str] = []

    result = await session.run_turn(
        "recover",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="system contract",
        status_callback=status.append,
        thinking_stream_callback=thinking.append,
    )

    assert result.final_text == "retry recovered"
    assert status == [
        "Codex gpt-5.6-sol へ送信中",
        "Codex が接続を再試行中",
    ]
    assert thinking == []
    await session.close()


@pytest.mark.asyncio
async def test_missing_persisted_thread_injects_fallback_context_once() -> None:
    class _MissingThreadClient(_ScriptedClient):
        async def request(
            self,
            method: str,
            params: Any = None,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            if method == "thread/resume":
                self.requests.append((method, params, timeout))
                raise CodexAppServerError(-32000, "thread not found")
            return await super().request(method, params, timeout)

    client = _MissingThreadClient(
        events=[
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "answer",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "recovered",
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "turn": {"status": "completed", "items": []},
                },
            },
        ]
    )
    session = CodexSession(
        cwd="/workspace",
        codex_bin="/opt/codex",
        stored_thread_id="deleted-thread",
        client_factory=lambda **_kwargs: client,
    )

    result = await session.run_turn(
        "lean current turn",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="system contract",
        resume_fallback_context="<untrusted_history>prior</untrusted_history>",
    )

    turn_params = next(
        params
        for method, params, _timeout in client.requests
        if method == "turn/start"
    )
    assert turn_params["input"] == [{
        "type": "text",
        "text": (
            "<untrusted_history>prior</untrusted_history>\n\n"
            "lean current turn"
        ),
    }]
    assert result.final_text == "recovered"
    await session.close()

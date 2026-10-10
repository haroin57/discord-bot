"""Request compatibility regressions for Claude Opus 5.5."""

import json

import anthropic
import httpx

from services.claude_api.model_settings import (
    THINKING_BINDING_BETA,
    model_betas,
    model_thinking,
)
from services.claude_api.request_shape import _build_stream_kwargs_impl
from services.oauth_manager import _sanitize_outgoing_request
from scripts.capture_claude_request import summarize_request


def test_opus55_replays_mutable_prefix_with_supported_thinking():
    original_betas = []
    assert model_betas("claude-opus-5-5", original_betas) == [THINKING_BINDING_BETA]
    assert original_betas == []
    assert model_thinking("claude-opus-5-5", disable=True) == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert model_thinking("claude-opus-4-8", disable=True) == {"type": "disabled"}


async def test_opus55_options_reach_the_serialized_sdk_request():
    received = []

    async def transport(request):
        received.append(request)
        return httpx.Response(200, json={
            "id": "msg_audit", "type": "message", "role": "assistant",
            "model": "claude-opus-5-5", "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    options = _build_stream_kwargs_impl(
        current_model="claude-opus-5-5", cached_system=[],
        messages=[{"role": "user", "content": "Return OK."}], extra_betas=[],
        output_config_override={"effort": "high"}, refusal_fallback_model=None,
        tools_for_api=[], extra_headers=None, fallback_used=False,
        server_side_fallback_enabled=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as transport_client:
        async with anthropic.AsyncAnthropic(api_key="test-key", http_client=transport_client) as client:
            await client.beta.messages.create(**options, timeout=120)
    body = json.loads(received[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"]["block_binding"]["prefix_mismatch_behavior"] == "drop_block"
    assert THINKING_BINDING_BETA in received[0].headers["anthropic-beta"]
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & body.keys()


async def test_transport_keeps_supported_encodings_and_rotates_request_id():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages", headers={
        "accept-encoding": "gzip, deflate", "x-client-request-id": "old",
        "x-stainless-async": "asyncio",
    })
    await _sanitize_outgoing_request(request)
    first_id = request.headers["x-client-request-id"]
    await _sanitize_outgoing_request(request)
    assert request.headers["accept-encoding"] == "gzip, deflate"
    assert request.headers["x-client-request-id"] != first_id
    assert "x-stainless-async" not in request.headers


def test_capture_excludes_credentials_account_ids_and_conversation_text():
    secret = "private-credential-or-conversation"
    summary = summarize_request({
        "model": "claude-opus-5-5",
        "system": [{"type": "text", "text": secret}],
        "messages": [{"role": "user", "content": secret}],
        "metadata": {"user_id": secret},
        "diagnostics": {"secret": secret},
    }, {"Authorization": secret, "Cookie": secret, "X-Api-Key": secret, "User-Agent": "claude-cli/2.1.296"})
    assert secret not in json.dumps(summary)
    assert summary["headers"] == {"user-agent": "claude-cli/2.1.296"}


def test_tool_order_is_stable_without_mutating_or_dropping_schemas():
    tools = [{"name": "zeta", "input_schema": {"type": "object"}},
             {"name": "alpha", "input_schema": {"type": "object"}}]
    original = list(tools)
    options = _build_stream_kwargs_impl(
        current_model="claude-opus-5-5", cached_system=[], messages=[], extra_betas=[],
        output_config_override=None, refusal_fallback_model=None, tools_for_api=tools,
        extra_headers=None, fallback_used=False, server_side_fallback_enabled=False,
    )
    assert tools == original
    assert options["tools"] == [tools[1], tools[0]]

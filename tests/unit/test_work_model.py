from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.work.model import WorkModelError, _isolated_codex_home, run_work_model


def _tools():
    return [{"name": "work_record", "description": "Save a durable result.",
             "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                              "required": ["text"], "additionalProperties": False}}]


def _call(request_id=1, call_id="call-1", name="work_record", arguments=None):
    return {"id": request_id, "method": "item/tool/call", "params": {
        "threadId": "thread-1", "turnId": "turn-1", "callId": call_id,
        "tool": name, "arguments": {"text": "saved"} if arguments is None else arguments,
    }}


def _completed():
    return [
        {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"type": "agentMessage", "phase": "final_answer", "text": "Confirmed result."},
        }},
        {"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"},
        }},
    ]


class FakeCodex:
    def __init__(self, events, **kwargs):
        self.kwargs = kwargs
        self.events = iter(events)
        self.initialize = AsyncMock()
        self.respond = AsyncMock()
        self.respond_error = AsyncMock()
        self.close = AsyncMock()
        self.requests = []
        self.initial_home_files = {p.name for p in Path(kwargs["codex_home"]).iterdir()}

    async def request(self, method, params, timeout=None):
        self.requests.append((method, params))
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method == "config/read":
            return {"config": {"mcp_servers": {"ambient": {"enabled": True}}, "plugins": {"external": {"enabled": True}}}}
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}, "model": params["model"],
                    "modelProvider": "openai", "approvalPolicy": "never",
                    "sandbox": {"type": "readOnly", "networkAccess": False}, "instructionSources": []}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)

    async def next_event(self, timeout=None):
        return next(self.events)


@pytest.fixture
def source_home(tmp_path):
    home = tmp_path / "source"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "test-token"}}))
    (home / "config.toml").write_text('developer_instructions="PRIVATE PERSONA"')
    (home / "AGENTS.md").write_text("PRIVATE FACTS")
    return home


async def _codex(source_home, events, execute=None, **kwargs):
    created = []

    def factory(**settings):
        client = FakeCodex(events, **settings)
        created.append(client)
        return client

    result = await run_work_model(
        provider="codex", model="test-model", prompt="Current work revision.",
        developer_instructions="Public persona only.", tools=_tools(),
        execute=execute or AsyncMock(return_value="Receipt saved."),
        codex_home=str(source_home), codex_bin="/test/codex", codex_client_factory=factory,
        **kwargs,
    )
    return result, created[0]


@pytest.mark.asyncio
async def test_codex_exposes_only_host_tools_without_ambient_config(source_home, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "not-for-the-child")
    monkeypatch.setenv("CODEX_API_KEY", "not-for-the-child")
    monkeypatch.setenv("CODEX_EXTRA_SETTINGS", "ambient-settings")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/ambient/config")
    result, client = await _codex(source_home, [_call(), *_completed()])
    assert result == "Confirmed result."
    assert client.initial_home_files == {"auth.json"}
    assert not Path(client.kwargs["codex_home"]).exists()
    assert client.kwargs["global_args"] == []
    assert client.kwargs["env"]["HOME"] != str(source_home)
    for key in ("DISCORD_TOKEN", "CODEX_API_KEY", "CODEX_EXTRA_SETTINGS", "XDG_CONFIG_HOME"):
        assert key not in client.kwargs["env"]
    assert client.initialize.call_args.kwargs["capabilities"] == {"experimentalApi": True}
    requests = dict(client.requests)
    thread = requests["thread/start"]
    assert thread["ephemeral"] is True
    assert thread["environments"] == requests["turn/start"]["environments"] == []
    assert thread["sandbox"] == "read-only"
    assert thread["developerInstructions"] == "Public persona only."
    assert thread["config"]["features.shell_tool"] is False
    assert thread["config"]["features.hooks"] is False
    assert thread["config"]['mcp_servers."ambient".enabled'] is False
    assert thread["config"]['plugins."external".enabled'] is False
    assert thread["dynamicTools"] == [{"type": "function", "name": "work_record",
        "description": "Save a durable result.", "inputSchema": _tools()[0]["input_schema"]}]
    assert requests["turn/start"]["input"] == [{"type": "text", "text": "Current work revision."}]
    assert "thread/resume" not in requests
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_codex_transport_duplicate_does_not_reexecute(source_home):
    execute = AsyncMock(return_value="stored")
    result, client = await _codex(source_home, [_call(), _call(2), *_completed()], execute=execute)
    execute.assert_awaited_once_with("work_record", {"text": "saved"})
    assert client.respond.await_count == 2
    assert result == "Confirmed result."


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    _call(name="Bash"),
    _call(arguments={"text": 2}),
    {"method": "item/started", "params": {"item": {"type": "mcpToolCall"}}},
])
async def test_codex_unadvertised_or_invalid_tools_fail_closed(source_home, event):
    execute = AsyncMock()
    with pytest.raises(WorkModelError):
        await _codex(source_home, [event, *_completed()], execute=execute)
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_work_interruption_propagates_before_next_effect(source_home):
    from services.work.runtime import WorkInterrupted

    interruption = WorkInterrupted("revision changed")
    execute = AsyncMock(side_effect=interruption)
    with pytest.raises(WorkInterrupted) as error:
        await _codex(source_home, [_call(), _call(2, "call-2"), *_completed()], execute=execute)
    assert error.value is interruption
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_native_permission_request_is_denied_without_user_prompt(source_home):
    event = {"id": 9, "method": "item/commandExecution/requestApproval", "params": {"command": "danger"}}
    _, client = await _codex(source_home, [event, *_completed()])
    client.respond_error.assert_awaited_once_with(9, -32000, "Only host Work tools are available")


def test_oauth_copy_is_private_and_only_uncontested_refresh_is_saved(source_home):
    with _isolated_codex_home(source_home) as (home, cwd):
        assert home.stat().st_mode & 0o777 == 0o700
        assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
        assert not (home / "config.toml").exists()
        assert not (home / "AGENTS.md").exists()
        (home / "auth.json").write_text('{"refreshed":true}')
    assert json.loads((source_home / "auth.json").read_text()) == {"refreshed": True}
    assert (source_home / "auth.json").stat().st_mode & 0o777 == 0o600
    with _isolated_codex_home(source_home) as (home, cwd):
        (home / "auth.json").write_text('{"refreshed":"older"}')
        (source_home / "auth.json").write_text('{"refreshed":"newer"}')
    assert json.loads((source_home / "auth.json").read_text()) == {"refreshed": "newer"}


class FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get_final_message(self):
        return self.response


class FakeClaude:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=self)
        self.close = AsyncMock()

    def stream(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs)))
        return FakeStream(next(self.responses))


def _claude_tool():
    return SimpleNamespace(stop_reason="tool_use", content=[{
        "type": "tool_use", "id": "tool-1", "name": "work_record", "input": {"text": "saved"},
    }])


@pytest.mark.asyncio
async def test_claude_uses_same_allowlist_and_fresh_history():
    client = FakeClaude([_claude_tool(), SimpleNamespace(stop_reason="end_turn", content=[{
        "type": "text", "text": "Confirmed Claude result.",
    }])])
    execute = AsyncMock(return_value="Receipt saved.")
    result = await run_work_model(
        provider="claude", model="claude-test", effort="high", prompt="Current revision.",
        tools=_tools(), execute=execute, claude_client_factory=AsyncMock(return_value=client),
    )
    assert result == "Confirmed Claude result."
    assert client.requests[0]["tools"] == _tools()
    assert client.requests[0]["messages"] == [{"role": "user", "content": "Current revision."}]
    assert client.requests[1]["messages"][-1]["content"][0]["content"] == "Receipt saved."
    assert client.requests[0]["output_config"] == {"effort": "high"}
    assert "fallbacks" not in client.requests[0]["extra_body"]
    execute.assert_awaited_once()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_claude_does_not_hide_a_work_boundary_exception():
    from services.work.runtime import WorkInterrupted

    client = FakeClaude([_claude_tool()])
    execute = AsyncMock(side_effect=WorkInterrupted())
    with pytest.raises(WorkInterrupted):
        await run_work_model(
            provider="claude", prompt="Current revision.", tools=_tools(), execute=execute,
            claude_client_factory=AsyncMock(return_value=client),
        )
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_claude_incomplete_result_is_not_finalized():
    client = FakeClaude([SimpleNamespace(stop_reason="max_tokens", content=[{"type": "text", "text": "cut"}])])
    with pytest.raises(WorkModelError, match="before completion"):
        await run_work_model(provider="claude", prompt="Current revision.", tools=[], execute=AsyncMock(),
            claude_client_factory=AsyncMock(return_value=client))


@pytest.mark.asyncio
async def test_remote_schema_resolution_and_duplicate_tools_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        await run_work_model(prompt="", tools=_tools() * 2, execute=AsyncMock())
    tools = _tools()
    tools[0]["input_schema"]["$ref"] = "https://external.invalid/schema.json"
    with pytest.raises(ValueError, match="self-contained"):
        await run_work_model(prompt="", tools=tools, execute=AsyncMock())


@pytest.fixture
def inline_image():
    from PIL import Image

    def build(mime_type="image/png"):
        formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(output, format=formats[mime_type])
        return {"mime_type": mime_type, "data": base64.b64encode(output.getvalue()).decode("ascii")}

    return build


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg", "image/webp", "image/gif"])
async def test_codex_receives_validated_image_as_inline_data_url(source_home, inline_image, mime_type):
    image = inline_image(mime_type)
    original = dict(image)
    _, client = await _codex(source_home, _completed(), images=[image])
    inputs = dict(client.requests)["turn/start"]["input"]
    assert inputs == [
        {"type": "text", "text": "Current work revision."},
        {"type": "image", "url": f"data:{mime_type};base64,{image['data']}"},
    ]
    assert image == original
    assert all(block["type"] != "localImage" for block in inputs)
    assert dict(client.requests)["turn/start"]["environments"] == []
    assert client.kwargs["stream_limit"] == 8 * 1024 * 1024 + len(image["data"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg", "image/webp", "image/gif"])
async def test_claude_receives_image_source_on_tool_free_verification(inline_image, mime_type):
    image = inline_image(mime_type)
    client = FakeClaude([SimpleNamespace(stop_reason="end_turn", content=[{
        "type": "text", "text": "The image was included in verification.",
    }])])
    await run_work_model(
        provider="claude", model="claude-test", prompt="Verify this source image.",
        images=[image], tools=[], execute=AsyncMock(),
        claude_client_factory=AsyncMock(return_value=client),
    )
    assert client.requests[0]["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "Verify this source image."},
        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image["data"]}},
    ]}]
    assert client.requests[0].get("tools", []) == []


@pytest.mark.asyncio
async def test_codex_tool_free_verification_keeps_all_four_source_images(source_home, inline_image):
    images = [inline_image(mime) for mime in ("image/png", "image/jpeg", "image/webp", "image/gif")]
    clients = []

    def factory(**kwargs):
        client = FakeCodex(_completed(), **kwargs)
        clients.append(client)
        return client

    await run_work_model(
        provider="codex", model="test-model", prompt="Verify all four source images.",
        images=images, tools=[], execute=AsyncMock(), codex_bin="/test/codex",
        codex_home=str(source_home), codex_client_factory=factory,
    )
    requests = dict(clients[0].requests)
    assert requests["thread/start"]["dynamicTools"] == []
    assert clients[0].kwargs["stream_limit"] == 8 * 1024 * 1024 + sum(len(image["data"]) for image in images)
    assert [item["url"] for item in requests["turn/start"]["input"][1:]] == [
        f"data:{image['mime_type']};base64,{image['data']}" for image in images
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"mime_type": "image/svg+xml", "data": "PHN2Zz48L3N2Zz4="},
    {"mime_type": "image/png", "data": "not base64"},
    {"mime_type": "image/png", "data": "日本語"},
    {"mime_type": "image/png", "data": ""},
    {"mime_type": "image/png", "data": "aGVsbG8="},
    {"mime_type": "image/jpeg", "data": "iVBORw0KGgo="},
    {"mime_type": "image/png", "data": 123},
    {"mime_type": [], "data": "iVBORw0KGgo="},
    "https://untrusted.invalid/image.png",
])
async def test_invalid_image_never_constructs_provider_client(invalid):
    factory = AsyncMock(side_effect=AssertionError("invalid image reached provider setup"))
    with pytest.raises(ValueError, match="Work image"):
        await run_work_model(
            provider="claude", prompt="Inspect image.", images=[invalid],
            tools=[], execute=AsyncMock(), claude_client_factory=factory,
        )
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_more_than_four_images_rejected_before_provider_setup(inline_image):
    image = inline_image()
    factory = AsyncMock()
    with pytest.raises(ValueError, match="at most four"):
        await run_work_model(
            provider="claude", prompt="Inspect images.", images=[image] * 5,
            tools=[], execute=AsyncMock(), claude_client_factory=factory,
        )
    factory.assert_not_called()


def test_decoded_image_size_limit_includes_final_base64_block(inline_image):
    from services.work.model import _validated_images

    image = inline_image()
    original = base64.b64decode(image["data"])
    padded = original + bytes(8 * 1024 * 1024 - len(original))
    at_limit = {"mime_type": "image/png", "data": base64.b64encode(padded).decode("ascii")}
    assert _validated_images([at_limit]) == [at_limit]
    # +1 decoded byte has the same encoded length at this boundary. This
    # confirms that encoded-length checks alone cannot enforce the byte cap.
    oversized = {"mime_type": "image/png", "data": base64.b64encode(padded + b"x").decode("ascii")}
    assert len(oversized["data"]) == len(at_limit["data"])
    with pytest.raises(ValueError, match="decoded size"):
        _validated_images([oversized])


def test_huge_encoded_image_is_rejected_before_base64_allocation(monkeypatch):
    from services.work import model

    monkeypatch.setattr(model.base64, "b64decode", Mock(side_effect=AssertionError("must reject before decoding")))
    data = "A" * (4 * ((8 * 1024 * 1024 + 2) // 3) + 4)
    with pytest.raises(ValueError, match="eight MiB"):
        model._validated_images([{"mime_type": "image/png", "data": data}])

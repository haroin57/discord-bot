from __future__ import annotations

import asyncio
import json

import pytest

from services.codex_app_server.client import (
    AsyncCodexAppServerClient,
    CodexAppServerError,
    CodexProcessError,
    build_codex_env,
)


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self.active_drains = 0
        self.max_active_drains = 0

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ValueError("closed")
        self.writes.append(data)

    async def drain(self) -> None:
        self.active_drains += 1
        self.max_active_drains = max(self.max_active_drains, self.active_drains)
        await asyncio.sleep(0)
        self.active_drains -= 1

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed

    @property
    def messages(self) -> list[dict]:
        return [json.loads(raw) for raw in self.writes]


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeWriter()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._waiter: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def wait(self) -> int:
        return await asyncio.shield(self._waiter)

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.exit(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit(-9)

    def feed_stdout(self, message: object) -> None:
        self.stdout.feed_data(json.dumps(message).encode() + b"\n")

    def feed_raw_stdout(self, data: bytes) -> None:
        self.stdout.feed_data(data + b"\n")

    def feed_stderr(self, line: str) -> None:
        self.stderr.feed_data(line.encode() + b"\n")

    def exit(self, returncode: int) -> None:
        if self.returncode is not None:
            return
        self.returncode = returncode
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._waiter.set_result(returncode)


class _SpawnHarness:
    def __init__(self) -> None:
        self.processes: list[_FakeProcess] = []
        self.calls: list[tuple[tuple, dict]] = []

    async def spawn(self, *args, **kwargs) -> _FakeProcess:
        process = _FakeProcess()
        self.processes.append(process)
        self.calls.append((args, kwargs))
        return process

    @property
    def process(self) -> _FakeProcess:
        return self.processes[-1]


@pytest.fixture
def spawn_harness(monkeypatch) -> _SpawnHarness:
    harness = _SpawnHarness()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", harness.spawn)
    return harness


async def _wait_for_messages(writer: _FakeWriter, count: int) -> list[dict]:
    for _ in range(100):
        messages = writer.messages
        if len(messages) >= count:
            return messages
        await asyncio.sleep(0)
    raise AssertionError(f"only received {len(writer.messages)} messages")


def test_build_codex_env_is_allowlisted_and_forces_oauth(tmp_path) -> None:
    source = {
        "HOME": "/home/bot",
        "PATH": "/usr/bin",
        "LANG": "ja_JP.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "HTTPS_PROXY": "http://proxy.invalid",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "CODEX_SQLITE_HOME": "/state/sqlite",
        "CODEX_EXPERIMENTAL_FLAG": "1",
        "CODEX_API_KEY": "codex-secret",
        "CODEX_ACCESS_TOKEN": "oauth-secret-must-come-from-disk",
        "OPENAI_API_KEY": "openai-secret",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "DISCORD_BOT_TOKEN": "discord-secret",
        "GITHUB_TOKEN": "github-secret",
        "UNRELATED": "drop-me",
    }

    result = build_codex_env(source, codex_home=tmp_path / "codex")

    assert result == {
        "HOME": "/home/bot",
        "PATH": "/usr/bin",
        "LANG": "ja_JP.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "HTTPS_PROXY": "http://proxy.invalid",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "CODEX_SQLITE_HOME": "/state/sqlite",
        "CODEX_EXPERIMENTAL_FLAG": "1",
        "CODEX_HOME": str(tmp_path / "codex"),
        "RUST_LOG": "warn",
    }


def test_build_codex_env_filters_overrides() -> None:
    result = build_codex_env(
        {"HOME": "/home/bot", "OPENAI_API_KEY": "old"},
        overrides={"TZ": "Asia/Tokyo", "OPENAI_API_KEY": "new"},
    )

    assert result == {
        "HOME": "/home/bot",
        "TZ": "Asia/Tokyo",
        "RUST_LOG": "warn",
    }


@pytest.mark.asyncio
async def test_start_uses_stdio_extra_args_and_sanitized_env(
    spawn_harness: _SpawnHarness,
) -> None:
    client = AsyncCodexAppServerClient(
        codex_bin="/opt/codex",
        codex_home="/state/codex",
        global_args=["--dangerously-bypass-approvals-and-sandbox"],
        extra_args=["--listen", "stdio://"],
        env={"HOME": "/home/bot", "OPENAI_API_KEY": "must-not-leak"},
        cwd="/workspace",
    )

    assert await client.start() is client
    assert await client.start() is client
    assert len(spawn_harness.calls) == 1
    args, kwargs = spawn_harness.calls[0]
    assert args == (
        "/opt/codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "app-server",
        "--listen",
        "stdio://",
    )
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert kwargs["cwd"] == "/workspace"
    assert kwargs["env"] == {
        "HOME": "/home/bot",
        "CODEX_HOME": "/state/codex",
        "RUST_LOG": "warn",
    }

    await client.close()


@pytest.mark.asyncio
async def test_initialize_performs_handshake(spawn_harness: _SpawnHarness) -> None:
    client = AsyncCodexAppServerClient(env={"PATH": "/usr/bin"})
    await client.start()
    process = spawn_harness.process

    task = asyncio.create_task(
        client.initialize(
            client_name="kanae_discord",
            client_title="Kanae Discord",
            client_version="2.0",
            capabilities={"experimentalApi": True},
        )
    )
    messages = await _wait_for_messages(process.stdin, 1)
    request = messages[0]
    assert request == {
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "kanae_discord",
                "title": "Kanae Discord",
                "version": "2.0",
            },
            "capabilities": {"experimentalApi": True},
        },
    }
    process.feed_stdout({"id": request["id"], "result": {"userAgent": "codex"}})

    assert await task == {"userAgent": "codex"}
    messages = await _wait_for_messages(process.stdin, 2)
    assert messages[1] == {"method": "initialized", "params": {}}
    with pytest.raises(RuntimeError, match="already initialized"):
        await client.initialize()
    await client.close()


@pytest.mark.asyncio
async def test_concurrent_requests_are_correlated_out_of_order(
    spawn_harness: _SpawnHarness,
) -> None:
    client = AsyncCodexAppServerClient()
    await client.start()
    process = spawn_harness.process

    first = asyncio.create_task(client.request("first", {"value": 1}))
    second = asyncio.create_task(client.request("second", {"value": 2}))
    messages = await _wait_for_messages(process.stdin, 2)
    by_method = {message["method"]: message["id"] for message in messages}
    process.feed_stdout({"id": by_method["second"], "result": {"answer": 2}})
    process.feed_stdout({"id": by_method["first"], "result": {"answer": 1}})

    assert await first == {"answer": 1}
    assert await second == {"answer": 2}
    assert process.stdin.max_active_drains == 1
    await client.close()


@pytest.mark.asyncio
async def test_notifications_and_server_requests_share_arrival_ordered_queue(
    spawn_harness: _SpawnHarness,
) -> None:
    client = AsyncCodexAppServerClient()
    await client.start()
    process = spawn_harness.process

    notification = {"method": "turn/started", "params": {"turn": {"id": "t1"}}}
    server_request = {
        "id": "approval-1",
        "method": "item/commandExecution/requestApproval",
        "params": {"command": "pwd"},
    }
    next_notification = {"method": "item/completed", "params": {"id": "i1"}}
    process.feed_stdout(notification)
    process.feed_stdout(server_request)
    process.feed_stdout(next_notification)

    assert await client.next_event(timeout=1) == notification
    assert await client.next_event(timeout=1) == server_request
    assert await client.next_event(timeout=1) == next_notification
    assert await client.next_event(timeout=0) is None
    await client.close()


@pytest.mark.asyncio
async def test_json_rpc_error_has_structured_fields(spawn_harness: _SpawnHarness) -> None:
    client = AsyncCodexAppServerClient()
    await client.start()
    process = spawn_harness.process

    task = asyncio.create_task(client.request("thread/start"))
    request = (await _wait_for_messages(process.stdin, 1))[0]
    process.feed_stdout(
        {
            "id": request["id"],
            "error": {"code": -32001, "message": "busy", "data": {"retry": True}},
        }
    )

    with pytest.raises(CodexAppServerError) as raised:
        await task
    assert raised.value.code == -32001
    assert raised.value.message == "busy"
    assert raised.value.data == {"retry": True}
    assert raised.value.request_id == request["id"]
    await client.close()


@pytest.mark.asyncio
async def test_timeout_cleans_pending_and_late_response_is_ignored(
    spawn_harness: _SpawnHarness,
) -> None:
    client = AsyncCodexAppServerClient()
    await client.start()
    process = spawn_harness.process

    with pytest.raises(TimeoutError, match="timed out"):
        await client.request("slow", timeout=0.001)
    first_request = process.stdin.messages[0]
    assert client._pending == {}
    process.feed_stdout({"id": first_request["id"], "result": {"late": True}})

    next_task = asyncio.create_task(client.request("fast", timeout=1))
    next_request = (await _wait_for_messages(process.stdin, 2))[1]
    process.feed_stdout({"id": next_request["id"], "result": {"ok": True}})
    assert await next_task == {"ok": True}
    assert next_request["id"] != first_request["id"]
    await client.close()


@pytest.mark.asyncio
async def test_notify_and_server_request_responses_have_correct_envelopes(
    spawn_harness: _SpawnHarness,
) -> None:
    client = AsyncCodexAppServerClient()
    await client.start()
    process = spawn_harness.process

    await asyncio.gather(
        client.notify("custom/event", {"x": 1}),
        client.respond("req-1", {"decision": "accept"}),
        client.respond_error("req-2", -32602, "invalid", {"field": "x"}),
    )
    messages = await _wait_for_messages(process.stdin, 3)

    assert {json.dumps(message, sort_keys=True) for message in messages} == {
        json.dumps({"method": "custom/event", "params": {"x": 1}}, sort_keys=True),
        json.dumps({"id": "req-1", "result": {"decision": "accept"}}, sort_keys=True),
        json.dumps(
            {
                "id": "req-2",
                "error": {
                    "code": -32602,
                    "message": "invalid",
                    "data": {"field": "x"},
                },
            },
            sort_keys=True,
        ),
    }
    assert process.stdin.max_active_drains == 1
    await client.close()


@pytest.mark.asyncio
async def test_stderr_drain_is_bounded(spawn_harness: _SpawnHarness) -> None:
    client = AsyncCodexAppServerClient(stderr_max_lines=3, stderr_line_limit=20)
    await client.start()
    process = spawn_harness.process

    process.feed_stderr("one")
    process.feed_stderr("two")
    process.feed_stderr("three")
    process.feed_stderr("x" * 100)
    for _ in range(100):
        if len(client.stderr_tail(10)) == 3 and client.stderr_tail(1)[0] != "three":
            break
        await asyncio.sleep(0)

    assert client.stderr_tail(10)[:2] == ["two", "three"]
    assert len(client.stderr_tail(1)[0]) <= 20
    assert client.stderr_tail(0) == []
    await client.close()


@pytest.mark.asyncio
async def test_process_exit_fails_all_pending_and_ends_event_stream(
    spawn_harness: _SpawnHarness,
) -> None:
    client = AsyncCodexAppServerClient()
    await client.start()
    process = spawn_harness.process

    first = asyncio.create_task(client.request("one", timeout=None))
    second = asyncio.create_task(client.request("two", timeout=None))
    await _wait_for_messages(process.stdin, 2)
    process.feed_stderr("fatal diagnostic")
    process.exit(17)

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, CodexProcessError) for result in results)
    assert all(result.returncode == 17 for result in results)
    assert client.is_alive is False
    with pytest.raises(CodexProcessError) as raised:
        await client.next_event(timeout=1)
    assert raised.value.returncode == 17
    assert raised.value.stderr_tail == ["fatal diagnostic"]
    await client.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_fails_pending(
    spawn_harness: _SpawnHarness,
) -> None:
    client = AsyncCodexAppServerClient()
    await client.start()
    process = spawn_harness.process
    pending = asyncio.create_task(client.request("long-running", timeout=None))
    await _wait_for_messages(process.stdin, 1)

    await asyncio.gather(client.close(), client.close())

    with pytest.raises(CodexProcessError, match="client closed"):
        await pending
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert client.is_alive is False
    await client.close()


@pytest.mark.asyncio
async def test_non_json_stdout_is_diagnostic_not_an_event(
    spawn_harness: _SpawnHarness,
) -> None:
    client = AsyncCodexAppServerClient()
    await client.start()
    process = spawn_harness.process
    process.feed_raw_stdout(b"not-json-and-no-secrets-are-copied")

    for _ in range(100):
        if client.stderr_tail():
            break
        await asyncio.sleep(0)
    assert client.stderr_tail() == ["<non-json on codex stdout: 34 bytes>"]
    assert await client.next_event(timeout=0) is None
    await client.close()

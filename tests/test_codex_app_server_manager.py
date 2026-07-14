from __future__ import annotations

import json
import stat
from typing import Any

import pytest

from services.codex_app_server import manager as manager_module
from services.codex_app_server.manager import (
    CodexSessionManager,
    _safe_mcp_url,
    build_app_server_extra_args,
    build_app_server_global_args,
    configured_mcp_endpoints,
)
from services.codex_app_server.session import CodexTurnResult


class _FakeSession:
    instances: list["_FakeSession"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.thread_id = kwargs.get("stored_thread_id")
        self.is_alive = True
        self.resolved_model = ""
        self.closed = False
        self.calls: list[dict[str, Any]] = []
        self.__class__.instances.append(self)

    async def run_turn(
        self,
        prompt: Any,
        *,
        model: str,
        effort: str,
        developer_instructions: str,
        **callbacks: Any,
    ) -> CodexTurnResult:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "effort": effort,
                "developer_instructions": developer_instructions,
                "callbacks": callbacks,
            }
        )
        self.resolved_model = model
        self.thread_id = self.thread_id or f"thread-{len(self.__class__.instances)}"
        return CodexTurnResult(
            final_text="ok",
            thread_id=self.thread_id,
            turn_id=f"turn-{len(self.calls)}",
            model=model,
            model_provider="openai",
            resumed=bool(self.kwargs.get("stored_thread_id")),
            sandbox=str(self.kwargs.get("sandbox") or ""),
            approval_policy=str(self.kwargs.get("approval_policy") or ""),
            token_usage_last={"outputTokens": 3},
            token_usage_total={"outputTokens": 5},
            model_context_window=258400,
        )

    async def close(self) -> None:
        self.closed = True
        self.is_alive = False


@pytest.fixture(autouse=True)
def fake_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSession.instances.clear()
    monkeypatch.setattr(manager_module, "CodexSession", _FakeSession)
    monkeypatch.setattr(
        manager_module,
        "build_app_server_extra_args",
        lambda model: ["-c", f'model="{model}"'],
    )


@pytest.mark.asyncio
async def test_manager_persists_thread_and_reuses_same_user_session(tmp_path) -> None:
    state_path = tmp_path / "codex-sessions.json"
    manager = CodexSessionManager(state_path=str(state_path), max_sessions=2)

    first = await manager.run_turn(
        100,
        200,
        "first",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )
    second = await manager.run_turn(
        100,
        200,
        "second",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )

    assert first.thread_id == second.thread_id == "thread-1"
    assert len(_FakeSession.instances) == 1
    assert [call["prompt"] for call in _FakeSession.instances[0].calls] == [
        "first",
        "second",
    ]
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    persisted_session = persisted["sessions"]["100:200"]
    updated_at = persisted_session.pop("updated_at")
    assert isinstance(updated_at, float)
    assert updated_at > 0
    assert persisted_session == {
        "thread_id": "thread-1",
        "model": "gpt-5.6-sol",
        "model_provider": "openai",
        "sandbox": "workspace-write",
        "approval_policy": "untrusted",
        "token_usage_last": {"outputTokens": 3},
        "token_usage_total": {"outputTokens": 5},
        "model_context_window": 258400,
        "compacted": False,
    }
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_manager_restores_thread_and_partitions_users(tmp_path) -> None:
    state_path = tmp_path / "codex-sessions.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "100:200": {
                        "thread_id": "persisted-thread",
                        "model": "gpt-5.6-sol",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manager = CodexSessionManager(state_path=str(state_path), max_sessions=3)

    restored = await manager.run_turn(
        100,
        200,
        "resume",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )
    separate_user = await manager.run_turn(
        100,
        201,
        "new user",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )

    assert restored.thread_id == "persisted-thread"
    assert _FakeSession.instances[0].kwargs["stored_thread_id"] == "persisted-thread"
    assert separate_user.thread_id != restored.thread_id
    assert manager.has_thread(100, 200)
    assert manager.has_thread(100, 201)


@pytest.mark.asyncio
async def test_manager_evicts_lru_process_but_keeps_durable_mapping(tmp_path) -> None:
    manager = CodexSessionManager(
        state_path=str(tmp_path / "codex-sessions.json"),
        max_sessions=1,
    )

    first = await manager.run_turn(
        100,
        200,
        "first user",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )
    first_session = _FakeSession.instances[0]
    await manager.run_turn(
        100,
        201,
        "second user",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )

    assert first_session.closed is True
    assert manager.context(100, 200)["thread_id"] == first.thread_id
    assert manager.has_thread(100, 201)

    cleared = await manager.clear_channel(100)
    assert cleared == 2
    assert manager.context(100, 200) is None
    assert manager.context(100, 201) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "https://example.com/mcp#signed-fragment",
        "file:///tmp/mcp.sock",
    ],
)
def test_mcp_url_rejects_argv_credentials_and_non_http(url: str) -> None:
    assert _safe_mcp_url(url) is None


def test_mcp_url_accepts_credential_free_http_endpoint() -> None:
    assert _safe_mcp_url("http://127.0.0.1:8013/mcp") == (
        "http://127.0.0.1:8013/mcp"
    )


def test_mcp_args_reset_global_table_and_use_unquoted_safe_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "second-opinion": {"url": "http://127.0.0.1:8014/mcp"},
                "invalid.name": {"url": "http://127.0.0.1:9999/mcp"},
            }
        }),
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        """
[mcp_servers.cloudflare]
url = "https://mcp.cloudflare.com/mcp"

[plugins."github@openai-curated"]
enabled = true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(manager_module, "BOT_DIR", str(tmp_path))
    monkeypatch.setattr(
        manager_module,
        "CODEX_APP_SERVER_MEM0_MCP_URL",
        "http://127.0.0.1:8013/mcp",
    )
    monkeypatch.setattr(
        manager_module,
        "CODEX_APP_SERVER_INHERIT_MCP_SERVERS",
        ("cloudflare",),
    )

    args = build_app_server_extra_args("gpt-5.6-sol")

    assert args[:2] == ["-c", "mcp_servers={}"]
    assert "features.apps=false" in args
    assert "features.remote_plugin=false" in args
    assert "apps._default.enabled=false" in args
    assert 'mcp_servers.kanae-mem0.url="http://127.0.0.1:8013/mcp"' in args
    assert 'mcp_servers.second-opinion.url="http://127.0.0.1:8014/mcp"' in args
    assert 'mcp_servers.cloudflare.url="https://mcp.cloudflare.com/mcp"' in args
    assert not any('mcp_servers."' in arg for arg in args)
    assert not any("invalid.name" in arg for arg in args)
    assert "mcp_servers.cloudflare.enabled=false" not in args
    assert "plugins.github@openai-curated.enabled=false" in args


def test_configured_mcp_endpoints_rejects_missing_or_unsafe_inherited_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(manager_module, "BOT_DIR", str(tmp_path))
    monkeypatch.setattr(manager_module, "CODEX_APP_SERVER_MEM0_MCP_URL", "")
    monkeypatch.setattr(
        manager_module,
        "CODEX_APP_SERVER_INHERIT_MCP_SERVERS",
        ("missing", "unsafe.name", "credentialed"),
    )
    monkeypatch.setattr(
        manager_module,
        "_global_codex_config",
        lambda: {
            "mcp_servers": {
                "credentialed": {"url": "https://user:pass@example.com/mcp"},
            }
        },
    )

    assert configured_mcp_endpoints() == {}


def test_bypass_flag_is_global_cli_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manager_module,
        "CODEX_APP_SERVER_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX",
        True,
    )
    assert build_app_server_global_args() == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]


@pytest.mark.asyncio
async def test_manager_passes_bypass_policy_to_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        manager_module,
        "CODEX_APP_SERVER_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX",
        True,
    )
    monkeypatch.setattr(
        manager_module, "CODEX_APP_SERVER_SANDBOX", "danger-full-access"
    )
    monkeypatch.setattr(manager_module, "CODEX_APP_SERVER_APPROVAL_POLICY", "never")
    manager = CodexSessionManager(
        state_path=str(tmp_path / "codex-sessions.json"),
        max_sessions=1,
    )

    await manager.run_turn(
        100,
        200,
        "bypass",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )

    kwargs = _FakeSession.instances[0].kwargs
    assert kwargs["global_args"] == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]
    assert kwargs["sandbox"] == "danger-full-access"
    assert kwargs["approval_policy"] == "never"


@pytest.mark.asyncio
async def test_manager_restarts_live_process_to_verify_model_change(tmp_path) -> None:
    manager = CodexSessionManager(
        state_path=str(tmp_path / "codex-sessions.json"),
        max_sessions=2,
    )
    first = await manager.run_turn(
        100,
        200,
        "sol turn",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )
    first_session = _FakeSession.instances[0]

    second = await manager.run_turn(
        100,
        200,
        "terra turn",
        model="gpt-5.6-terra",
        effort="xhigh",
        developer_instructions="contract",
    )

    assert first_session.closed is True
    assert len(_FakeSession.instances) == 2
    assert _FakeSession.instances[1].kwargs["stored_thread_id"] == first.thread_id
    assert second.thread_id == first.thread_id


@pytest.mark.asyncio
async def test_manager_restarts_live_process_when_instructions_change(tmp_path) -> None:
    manager = CodexSessionManager(
        state_path=str(tmp_path / "codex-sessions.json"),
        max_sessions=2,
    )
    first = await manager.run_turn(
        100,
        200,
        "before skill",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="base contract",
    )
    first_session = _FakeSession.instances[0]

    second = await manager.run_turn(
        100,
        200,
        "after skill",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="base contract\n\nactive inshi-coach",
    )

    assert first_session.closed is True
    assert len(_FakeSession.instances) == 2
    assert _FakeSession.instances[1].kwargs["stored_thread_id"] == first.thread_id
    assert second.thread_id == first.thread_id
    assert _FakeSession.instances[1].calls[0]["developer_instructions"].endswith(
        "active inshi-coach"
    )


@pytest.mark.asyncio
async def test_manager_clear_thread_only_removes_requested_user(tmp_path) -> None:
    manager = CodexSessionManager(
        state_path=str(tmp_path / "codex-sessions.json"),
        max_sessions=3,
    )
    await manager.run_turn(
        100,
        200,
        "first user",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )
    await manager.run_turn(
        100,
        201,
        "second user",
        model="gpt-5.6-sol",
        effort="xhigh",
        developer_instructions="contract",
    )

    assert await manager.clear_thread(100, 200) is True
    assert manager.context(100, 200) is None
    assert manager.context(100, 201) is not None
    assert await manager.clear_thread(100, 200) is False

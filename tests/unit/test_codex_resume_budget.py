"""Resume durable context without replaying the full display transcript."""

from unittest.mock import AsyncMock

import pytest

from services.codex_app_server.session import CodexSession


@pytest.mark.asyncio
@pytest.mark.parametrize("times_out", [False, True])
async def test_large_durable_thread_keeps_identity_without_history_replay(times_out, caplog):
    calls = []

    async def request(method, params, timeout):
        calls.append((method, params, timeout))
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        assert method == "thread/resume", "Existing context must not be replaced"
        assert params["excludeTurns"] is True
        assert 30 < timeout <= 120
        if times_out:
            raise TimeoutError("slow resume")
        return {
            "thread": {"id": "large-durable-thread", "turns": []},
            "model": "gpt-6-astra", "modelProvider": "openai",
        }

    client = AsyncMock()
    client.is_alive = True
    client.request.side_effect = request
    session = CodexSession(
        cwd="/workspace", codex_bin="/opt/codex", stored_thread_id="large-durable-thread",
        client_factory=lambda **kwargs: client,
    )
    try:
        if times_out:
            with pytest.raises(TimeoutError, match="slow resume"):
                await session.ensure_started(model="gpt-6-astra", developer_instructions="contract")
            assert "thread/resume timed out" in caplog.text
            assert session.resumed is False
        else:
            assert await session.ensure_started(
                model="gpt-6-astra", developer_instructions="contract",
            ) == "large-durable-thread"
            assert session.resumed is True
            assert session.resolved_model == "gpt-6-astra"
        assert session.thread_id == "large-durable-thread"
        assert [c[0] for c in calls] == ["account/read", "thread/resume"]
    finally:
        await session.close()

from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from scripts import experiment_replay_windows as replay


class FakeStream:
    def __init__(self, error=None):
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"code":"SILVER-42","retry_limit":7}')],
            stop_reason="end_turn", usage=SimpleNamespace(input_tokens=4,
                cache_read_input_tokens=10, cache_creation_input_tokens=20, output_tokens=24),
        )


def configure(monkeypatch, errors):
    from services.claude_api import client_factory
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        return FakeStream(errors[len(calls) - 1] if len(calls) <= len(errors) else None)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)), close=AsyncMock())
    client.with_options = lambda **kwargs: client
    monkeypatch.setattr(client_factory, "make_api_client", AsyncMock(return_value=client))
    monkeypatch.setattr(replay, "load_session", lambda *args: SimpleNamespace(
        load_api_replay_channel=lambda *args: ([{"role":"user", "content":"public synthetic"}], "", "")))
    return calls, client


async def test_completed_responses_are_checkpointed_with_environment_restored(monkeypatch, tmp_path):
    calls, client = configure(monkeypatch, [])
    monkeypatch.setenv("CLAUDE_API_REPLAY_EXTRA_ROWS", "old-value")
    output = tmp_path / "results.json"
    report = await replay.run(SimpleNamespace(baseline=None, payload=None, output=output, request_gap=0))
    saved = json.loads(output.read_text())
    assert saved == report
    assert saved["status"] == "complete"
    assert len(saved["results"]) == saved["requests_attempted"] == len(calls) == 18
    assert all(row["quality_pass"] for row in saved["results"])
    import os
    assert os.environ["CLAUDE_API_REPLAY_EXTRA_ROWS"] == "old-value"
    client.close.assert_awaited_once()


async def test_failure_keeps_completed_usage_without_exposing_error_body(monkeypatch, tmp_path):
    calls, client = configure(monkeypatch, [None, RuntimeError("secret-provider-payload")])
    output = tmp_path / "results.json"
    with pytest.raises(RuntimeError):
        await replay.run(SimpleNamespace(baseline=None, payload=None, output=output, request_gap=0))
    saved = json.loads(output.read_text())
    assert saved["status"] == "failed"
    assert saved["requests_attempted"] == len(calls) == 2
    assert len(saved["results"]) == 1
    assert saved["failed_attempts"][0]["usage"] == "unknown"
    assert "secret" not in output.read_text()
    client.close.assert_awaited_once()


async def test_long_retry_after_is_not_retried_early(monkeypatch, tmp_path):
    class RateLimit(RuntimeError):
        status_code = 429
        response = SimpleNamespace(headers={"retry-after": "1800"})

    calls, _ = configure(monkeypatch, [RateLimit("quota")])
    output = tmp_path / "results.json"
    with pytest.raises(RateLimit):
        await replay.run(SimpleNamespace(baseline=None, payload=None, output=output, request_gap=0))
    saved = json.loads(output.read_text())
    assert saved["requests_attempted"] == len(calls) == 1
    assert saved["failed_attempts"][0]["retry_after_seconds"] == "1800"
    assert saved["results"] == []

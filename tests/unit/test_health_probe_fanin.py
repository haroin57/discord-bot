import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest

from services.health_server import HealthServer


@pytest.mark.asyncio
async def test_overlapping_probes_share_work_but_do_not_cache_completed_health(monkeypatch):
    server = HealthServer(MagicMock())
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def probe(*, bot):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return {"status": "ok" if calls == 1 else "degraded"}

    monkeypatch.setattr("services.health.get_readiness_snapshot", probe)
    requests = [asyncio.create_task(server._handle_ready(None)) for _ in range(32)]
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        # /live remains independent while the shared dependency probe blocks.
        assert (await server._handle_live(None)).status == 200
        assert calls == 1
    finally:
        release.set()
    assert all(response.status == 200 for response in await asyncio.gather(*requests))
    assert (await server._handle_health(None)).status == 503
    assert calls == 2


@pytest.mark.asyncio
async def test_cancelling_one_probe_does_not_cancel_other_waiters(monkeypatch):
    server = HealthServer(MagicMock())
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def probe(*, bot):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return {"status": "ok"}

    monkeypatch.setattr("services.health.get_readiness_snapshot", probe)
    first = asyncio.create_task(server._handle_ready(None))
    second = asyncio.create_task(server._handle_ready(None))
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not server._readiness_task.cancelled()
    finally:
        release.set()
    assert (await second).status == 200
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_shared_probe_is_retried_on_next_request(monkeypatch):
    server = HealthServer(MagicMock())
    def broken(*, bot):
        raise OSError("dependency failed")
    monkeypatch.setattr("services.health.get_readiness_snapshot", broken)
    failed = await server._handle_ready(None)
    assert failed.status == 503
    assert json.loads(failed.body)["subsystems"]["readiness"]["ok"] is False
    monkeypatch.setattr("services.health.get_readiness_snapshot", lambda **_: {"status": "ok"})
    assert (await server._handle_ready(None)).status == 200

"""Real loopback HTTP and native LanceDB parity; no external APIs or credentials."""

import asyncio
import threading

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from services.offload import knowledge_client as remote
from services.offload import knowledge_server as server


def vector(index):
    values = [0.0] * 1536
    values[index] = 1.0
    return values


@pytest.mark.asyncio
async def test_native_search_over_http_has_identical_ranking_and_metadata(tmp_path, monkeypatch):
    from services.invest_kb import client
    from services.invest_kb import search
    path = str(tmp_path / "kb")
    rows = [{
        "id": f"test-{i}", "text": f"body {i}", "summary": f"summary {i}", "keywords": "", "entities": "",
        "vector_body": vector(i), "vector_summary": vector(1 - i),
        "source": "test", "source_id": str(i), "source_url": f"https://source.test/{i}",
        "title": f"title {i}", "lang": "ja", "license": "test", "published_at": 0, "category": "investment",
    } for i in range(2)]
    table = client.get_table(path)
    table.add(rows)
    token = "knowledge-token-" * 3
    app = server.create_app(token=token, db_path=path)
    async with TestServer(app) as http:
        monkeypatch.setenv("INVEST_KB_HTTP_URL", str(http.make_url("/")).rstrip("/"))
        monkeypatch.setenv("INVEST_KB_HTTP_TOKEN", token)
        expected = [search._vector_search_sync(table, v, col, 5) for v in [vector(0), vector(1)] for col in ("vector_body", "vector_summary")]
        actual = await remote.search_vectors([vector(0), vector(1)], 5)
        assert actual == expected
        assert actual[0][0]["metadata"]["source_url"] == "https://source.test/0"
        async with ClientSession() as session:
            async with session.get(http.make_url("/healthz")) as response:
                assert response.status == 401
            async with session.post(http.make_url("/v1/invest-kb/search"), json={"version": 1, "vectors": [[1]], "limit": 5}, headers={"Authorization": f"Bearer {token}"}) as response:
                assert response.status == 400


@pytest.mark.asyncio
async def test_service_bounds_native_queue_when_client_disconnects(monkeypatch):
    started, release = threading.Event(), threading.Event()
    def blocked(*_args):
        started.set()
        assert release.wait(5)
        return [[], []]
    monkeypatch.setattr(server, "_search", blocked)
    token = "knowledge-token-" * 3
    app = server.create_app(token=token, db_path="unused", max_pending=1, table_loader=lambda _: object())
    async with TestServer(app) as http, ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        body = {"version": 1, "vectors": [vector(0)], "limit": 5}
        task = asyncio.create_task(session.post(http.make_url("/v1/invest-kb/search"), json=body, headers=headers))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            async with session.post(http.make_url("/v1/invest-kb/search"), json=body, headers=headers) as response:
                assert response.status == 503
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_database_fails_startup_without_creating_empty_kb(tmp_path):
    missing = tmp_path / "missing"
    app = server.create_app(token="test" * 10, db_path=str(missing))
    http = TestServer(app)
    try:
        with pytest.raises(RuntimeError, match="directory does not exist"):
            await http.start_server()
    finally:
        await http.close()
    assert not missing.exists()

"""The hook daemon is a stdlib proxy; the search runs in the shared mem0 MCP."""

from __future__ import annotations

import asyncio
import http.server
import importlib
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from services.mem0 import hook_http


@pytest.fixture
def fake_search(monkeypatch):
    calls = []

    async def search(query, user_id, **kwargs):
        calls.append((query, user_id, kwargs))
        return [{"memory": f"hit:{query}"}]

    import services.mem0.format as fmt
    import services.mem0.search as search_mod

    monkeypatch.setattr(search_mod, "search_memories_smart", search)
    monkeypatch.setattr(
        fmt, "format_memories_for_prompt", lambda rs: "|".join(r["memory"] for r in rs)
    )
    return calls


def _app():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("mem0-test")
    hook_http.register_hook_route(mcp)
    return mcp.streamable_http_app()


async def _post(
    body: bytes,
    headers: dict[str, str] | None = None,
    base_url: str = "http://127.0.0.1:8013",
) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    hdrs = {"X-Kanae-Hook": "1"} if headers is None else headers
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as c:
        return await c.post(hook_http.HOOK_ROUTE, content=body, headers=hdrs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "base_url"),
    [
        ({}, "http://127.0.0.1:8013"),  # no hook header (CORS-simple POST)
        (
            {"X-Kanae-Hook": "1", "Origin": "http://evil.example"},
            "http://127.0.0.1:8013",
        ),
        ({"X-Kanae-Hook": "1"}, "http://evil.example:8013"),  # DNS rebinding Host
        ({"X-Kanae-Hook": "1"}, "http://127.0.0.1.evil.example:8013"),
    ],
)
async def test_route_rejects_untrusted_requests(fake_search, headers, base_url):
    resp = await _post(b"secret", headers=headers, base_url=base_url)
    assert resp.status_code == 403
    assert fake_search == []


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["http://localhost:8013", "http://[::1]:8013"])
async def test_route_accepts_loopback_aliases(fake_search, base_url):
    resp = await _post(b"q", base_url=base_url)
    assert resp.status_code == 200 and resp.text == "hit:q"


@pytest.mark.asyncio
async def test_route_uses_former_daemon_arguments(fake_search):
    resp = await _post("先輩の好きな酒".encode())
    assert resp.status_code == 200
    assert resp.text == "hit:先輩の好きな酒"
    assert fake_search == [
        (
            "先輩の好きな酒",
            "573489206240870410",
            {"limit": 4, "rewrite": False, "rerank": False, "include_core": True},
        )
    ]


@pytest.mark.asyncio
async def test_route_blank_query_skips_search(fake_search):
    resp = await _post(b"  \n")
    assert resp.status_code == 200 and resp.text == ""
    assert fake_search == []


@pytest.mark.asyncio
async def test_route_search_failure_degrades_to_empty(monkeypatch):
    import services.mem0.search as search_mod

    async def boom(*_a, **_k):
        raise RuntimeError("chroma down")

    monkeypatch.setattr(search_mod, "search_memories_smart", boom)
    resp = await _post(b"q")
    assert resp.status_code == 200 and resp.text == ""


@pytest.mark.asyncio
async def test_route_truncates_oversized_query(fake_search):
    await _post(b"a" * (hook_http.MAX_QUERY_BYTES + 100))
    assert len(fake_search[0][0]) == hook_http.MAX_QUERY_BYTES


def _daemon_module(monkeypatch, sock, url):
    monkeypatch.setenv("MEM0_HOOK_SOCK", str(sock))
    monkeypatch.setenv("MEM0_HOOK_URL", url)
    monkeypatch.setenv("MEM0_HOOK_HTTP_TIMEOUT", "1")
    sys.modules.pop("mem0_hook_daemon", None)
    return importlib.import_module("mem0_hook_daemon")


async def _ask(sock, query: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(query + b"\n")
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


@pytest.fixture
def http_echo():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            out = (
                b"block:" + body
                if self.headers.get("X-Kanae-Hook") == "1"
                else b"no-header"
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook/search"
    server.shutdown()


@pytest.mark.asyncio
async def test_daemon_proxies_socket_query_to_http(tmp_path, monkeypatch, http_echo):
    sock = tmp_path / "hook.sock"
    mod = _daemon_module(monkeypatch, sock, http_echo)
    server = await asyncio.start_unix_server(mod.handle, path=str(sock))
    async with server:
        assert await _ask(sock, "記憶".encode()) == "block:記憶".encode()
        assert await _ask(sock, b"") == b""


@pytest.mark.asyncio
async def test_daemon_returns_empty_when_backend_is_down(tmp_path, monkeypatch, capsys):
    sock = tmp_path / "hook.sock"
    mod = _daemon_module(monkeypatch, sock, "http://127.0.0.1:9/hook/search")
    server = await asyncio.start_unix_server(mod.handle, path=str(sock))
    async with server:
        assert await _ask(sock, b"q") == b""
        assert await _ask(sock, b"q2") == b""
    err = capsys.readouterr().err
    # The failure is visible in the journal, rate-limited to one line.
    assert err.count("backend failed") == 1


def test_daemon_stays_stdlib_only_and_small():
    probe = (
        "import sys, mem0_hook_daemon\n"
        "heavy = [m for m in sys.modules if m.split('.')[0] in "
        "('services', 'mem0', 'chromadb', 'numpy', 'openai', 'anthropic')]\n"
        "rss = [l for l in open('/proc/self/status') if l.startswith('VmRSS')][0]\n"
        "print(heavy, int(rss.split()[1]))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out[0] == "[]"
    assert int(out[-1]) < 40_000  # kB; the former daemon sat around 300MB

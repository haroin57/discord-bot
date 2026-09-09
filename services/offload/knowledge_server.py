"""Read-only native vector service for an isolated VM/container with LanceDB.

Run ``python -m services.offload.knowledge_server``. Mount a populated KB at
KNOWLEDGE_DB_PATH. No Discord token, OAuth credentials, MCP or Mem0 is needed.
All native queries are serialized on one executor to bound RAM/CPU, even if a
request disconnects. Queue admission is bounded and returns 503 on overload.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import web

from services.offload.knowledge_client import MAX_RESPONSE_BYTES, validate_vectors

log = logging.getLogger(__name__)


def _open_table(path: str):
    import lancedb

    if not Path(path).is_dir():
        raise RuntimeError("knowledge database directory does not exist")
    db = lancedb.connect(path)
    if "invest_kb" not in db.list_tables().tables:
        raise RuntimeError("knowledge database has no invest_kb table")
    return db.open_table("invest_kb")


def _search(table, vectors: list[list[float]], limit: int) -> list[list[dict]]:
    from services.invest_kb.search import _vector_search_sync

    # Read one table version for all variants. Ingestion publishes new versions
    # atomically on this host; no local database is created by a client request.
    table.checkout_latest()
    if table.count_rows() == 0:
        return [[] for _ in range(len(vectors) * 2)]
    return [
        _vector_search_sync(table, vector, column, limit)
        for vector in vectors for column in ("vector_body", "vector_summary")
    ]


def create_app(*, token: str, db_path: str, max_pending: int = 4, table_loader=None) -> web.Application:
    if len(token) < 32 or not token.isascii():
        raise ValueError("KNOWLEDGE_AUTH_TOKEN requires at least 32 ASCII characters")
    if not 1 <= max_pending <= 32:
        raise ValueError("KNOWLEDGE_MAX_PENDING must be between 1 and 32")
    loader = table_loader or _open_table
    table = None
    executor = None
    pending = 0

    @web.middleware
    async def authenticate(request, handler):
        supplied = request.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, f"Bearer {token}".encode("utf-8")):
            return web.json_response({"error": "unauthorized"}, status=401)
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(middlewares=[authenticate], client_max_size=1024 * 1024)

    async def lifecycle(_app):
        nonlocal table, executor
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="knowledge-query")
        try:
            table = await asyncio.get_running_loop().run_in_executor(executor, loader, db_path)
            yield
        finally:
            # Wait for native queries after disconnect/cancellation before exit.
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)

    async def health(_request):
        return web.json_response({"ok": table is not None, "version": 1})

    async def search(request):
        nonlocal pending
        if pending >= max_pending:
            return web.json_response({"error": "knowledge service busy"}, status=503, headers={"Retry-After": "1"})
        # Admission covers body parsing too, so a burst cannot allocate many
        # request bodies before reaching the native executor's queue.
        pending += 1
        native = None
        try:
            data = await asyncio.wait_for(request.json(), timeout=5)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("protocol version must be 1")
            vectors, limit = validate_vectors(data.get("vectors"), data.get("limit"))
            native = asyncio.get_running_loop().run_in_executor(executor, _search, table, vectors, limit)
            hits = await asyncio.shield(native)
            body = json.dumps({"version": 1, "hits": hits}, ensure_ascii=False, allow_nan=False)
            if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
                return web.json_response({"error": "knowledge result too large"}, status=413)
            return web.Response(text=body, content_type="application/json")
        except (ValueError, UnicodeError, OverflowError, asyncio.TimeoutError):
            return web.json_response({"error": "invalid search request"}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            log.error("knowledge native query failed")
            return web.json_response({"error": "knowledge service unavailable"}, status=503)
        finally:
            if native is not None and not native.done():
                # Keep capacity reserved until C++ work finishes, even when the
                # waiting HTTP handler is cancelled. Retrieve orphan errors.
                def completed(future):
                    nonlocal pending
                    pending -= 1
                    if not future.cancelled():
                        future.exception()
                native.add_done_callback(completed)
            else:
                pending -= 1

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/healthz", health)
    app.router.add_post("/v1/invest-kb/search", search)
    return app


def main() -> None:
    app = create_app(
        token=os.environ.get("KNOWLEDGE_AUTH_TOKEN", ""),
        db_path=os.environ.get("KNOWLEDGE_DB_PATH", "/data/invest_kb"),
        max_pending=int(os.environ.get("KNOWLEDGE_MAX_PENDING", "4")),
    )
    web.run_app(app, host=os.environ.get("KNOWLEDGE_HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8095")), access_log=None)


if __name__ == "__main__":
    main()

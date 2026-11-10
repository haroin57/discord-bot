"""One HTTP round trip for all query variants and both LanceDB vector columns.

Embedding, rewrite, RRF, filtering and rerank stay with the existing pipeline.
An explicitly selected remote backend never opens an empty local database on
failure; the preprocessing caller already treats unavailable KB as optional.
"""

from __future__ import annotations

import asyncio
import json
import math
import os

import aiohttp

from services.offload.endpoint import validate_endpoint

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_VECTORS = 10
MAX_LIMIT = 100
DIMENSIONS = 1536


def remote_search_enabled() -> bool:
    mode = os.environ.get("INVEST_KB_VECTOR_BACKEND", "local")
    if mode not in {"local", "http"}:
        raise ValueError("INVEST_KB_VECTOR_BACKEND must be local or http")
    return mode == "http"


def validate_vectors(vectors: object, limit: object) -> tuple[list[list[float]], int]:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit must be between 1 and 100")
    if not isinstance(vectors, list) or not 1 <= len(vectors) <= MAX_VECTORS:
        raise ValueError("expected 1 to 10 vectors")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != DIMENSIONS:
            raise ValueError("embedding dimension must be 1536")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise ValueError("embeddings must contain finite numbers")
        if not any(vector):
            raise ValueError("cosine search requires nonzero vectors")
    return vectors, limit


def validate_results(payload: object, count: int, limit: int) -> list[list[dict]]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("knowledge service protocol mismatch")
    hits = payload.get("hits")
    if not isinstance(hits, list) or len(hits) != count * 2:
        raise RuntimeError("knowledge service result count mismatch")
    for rows in hits:
        if not isinstance(rows, list) or len(rows) > limit:
            raise RuntimeError("knowledge service result limit exceeded")
        for row in rows:
            if (
                not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not isinstance(row.get("memory"), str)
                or not isinstance(row.get("metadata"), dict)
                or any(type(row.get(k)) not in (float, int) or not math.isfinite(row[k]) for k in ("score", "cosine_score"))
            ):
                raise RuntimeError("knowledge service invalid result")
    return hits


async def search_vectors(vectors: list[list[float]], limit: int) -> list[list[dict]]:
    validate_vectors(vectors, limit)
    url = validate_endpoint(os.environ.get("INVEST_KB_HTTP_URL", ""), name="INVEST_KB_HTTP_URL")
    token = os.environ.get("INVEST_KB_HTTP_TOKEN", "")
    if len(token) < 32:
        raise ValueError("INVEST_KB_HTTP_TOKEN must contain at least 32 characters")
    timeout = float(os.environ.get("INVEST_KB_HTTP_TIMEOUT_S", "20"))
    if not 0 < timeout <= 120:
        raise ValueError("INVEST_KB_HTTP_TIMEOUT_S must be in (0, 120]")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(
                url + "/v1/invest-kb/search",
                headers={"Authorization": f"Bearer {token}"},
                json={"version": 1, "vectors": vectors, "limit": limit},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"knowledge service HTTP {response.status}")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise RuntimeError("knowledge service response too large")
                return validate_results(json.loads(data), len(vectors), limit)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
        raise RuntimeError("knowledge service request failed") from None

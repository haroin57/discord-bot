"""OpenAI batch embedder for invest_kb (text-embedding-3-small, 1536D).

Picked to match Mem0's embedder so cosine distance is comparable across the
two stores. AsyncOpenAI with retry + batch chunking.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

log = logging.getLogger(__name__)

EMBED_MODEL = os.environ.get("INVEST_KB_EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = 1536
BATCH = int(os.environ.get("INVEST_KB_EMBED_BATCH", "100"))
MAX_RETRIES = 5

_client_cache: dict = {"client": None}


def _get_client():
    if _client_cache["client"] is None:
        from openai import AsyncOpenAI
        _client_cache["client"] = AsyncOpenAI()  # picks up OPENAI_API_KEY
    return _client_cache["client"]


async def _embed_one_batch(texts: list[str]) -> list[list[float]]:
    client = _get_client()
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.embeddings.create(model=EMBED_MODEL, input=texts)
            return [d.embedding for d in resp.data]
        except Exception as exc:  # noqa: BLE001 — retry on any transient
            if attempt == MAX_RETRIES - 1:
                raise
            log.warning(
                "embedding batch failed (attempt %d/%d): %s",
                attempt + 1, MAX_RETRIES, exc,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    return []


async def embed_batch(texts: Iterable[str]) -> list[list[float]]:
    """Embed a list of strings, returning vectors in input order."""
    out: list[list[float]] = []
    buf = list(texts)
    for i in range(0, len(buf), BATCH):
        chunk = buf[i : i + BATCH]
        # OpenAI rejects empty strings; substitute a single space so dimensions
        # line up. Callers should ideally filter empties before reaching here.
        safe = [t if t.strip() else " " for t in chunk]
        out.extend(await _embed_one_batch(safe))
    return out

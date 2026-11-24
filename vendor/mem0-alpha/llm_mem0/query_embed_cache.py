"""Short-TTL query-embedding cache + batch pre-warm (explore-C #6).

The recall hot path embeds *every* expanded query variant separately: each
``mem.search(q)`` runs ``embedding_model.embed(q, "search")`` → one embeddings
HTTP round trip, and ``search_memories``'s refill loop re-embeds the same ``q``
on every widening pass. explore-C measured ~194 embeddings/day this way.

This module collapses those round trips without changing a single returned
vector:

* **TTL cache** — ``embedding_model.embed`` is wrapped (search action only;
  add/update embeds pass straight through so ingest is untouched). Repeated
  embeds of the same text within the TTL reuse the first result. Embeddings are
  deterministic for a given model + input, so this is output-invariant.
* **Batch pre-warm** — before the multi-query fan-out, all variants are embedded
  in a single ``input=[…]`` call and stored in the cache. OpenAI-style
  embedders return per-item vectors identical to one-by-one calls, so the fan
  out then hits a warm cache and issues zero extra embedding round trips.

Everything is guarded: no embedder, a non-batchable embedder, or any error
falls back to the original per-call behaviour (never wrong, at worst as slow as
before). Disable entirely with ``MEM0_QUERY_EMBED_CACHE_TTL_S=0``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

log = logging.getLogger(__name__)

_CACHE_ATTR = "_kanae_query_embed_cache"


class QueryEmbeddingCache:
    """Wraps an embedding model's ``embed`` with a thread-safe TTL cache."""

    def __init__(self, embedder, ttl_s: float, maxsize: int):
        self._embedder = embedder
        self._orig_embed = embedder.embed  # captured BEFORE we shadow it
        self._ttl = float(ttl_s)
        self._maxsize = max(1, int(maxsize))
        self._store: "OrderedDict[tuple, tuple[float, object]]" = OrderedDict()
        self._lock = threading.Lock()
        # Observability (used by offline benchmarks / tests).
        self.hits = 0
        self.misses = 0
        self.passthrough = 0
        self.batch_calls = 0

    # -- key / store helpers -------------------------------------------------
    @staticmethod
    def _key(text, memory_action):
        # OpenAIEmbedding.embed normalises "\n" → " " before sending; mirror it
        # so a warm entry matches what a solo embed(text) would key on.
        return (memory_action, (text or "").replace("\n", " "))

    def _get_locked(self, key, now):
        entry = self._store.get(key)
        if entry is None:
            return None
        expiry, vec = entry
        if expiry < now:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return vec

    def _put_locked(self, key, vec, now):
        self._store[key] = (now + self._ttl, vec)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    # -- public embed (installed in place of embedding_model.embed) ----------
    def embed(self, text, memory_action=None):
        # Only cache the search path; ingest ("add"/"update") stays untouched.
        if self._ttl <= 0 or memory_action != "search":
            self.passthrough += 1
            return self._orig_embed(text, memory_action)
        now = time.monotonic()
        key = self._key(text, memory_action)
        with self._lock:
            vec = self._get_locked(key, now)
            if vec is not None:
                self.hits += 1
                return vec
        vec = self._orig_embed(text, memory_action)  # compute outside the lock
        with self._lock:
            self.misses += 1
            self._put_locked(key, vec, time.monotonic())
        return vec

    # -- batch pre-warm ------------------------------------------------------
    def warm(self, texts, memory_action: str = "search"):
        """Embed every not-yet-cached text in one round trip and store them."""
        if self._ttl <= 0:
            return
        now = time.monotonic()
        want = []
        seen_keys = set()
        for t in texts:
            if not t:
                continue
            key = self._key(t, memory_action)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            with self._lock:
                if self._get_locked(key, now) is not None:
                    continue
            want.append(t)
        if not want:
            return
        vectors = self._embed_many(want, memory_action)
        if vectors is None:
            # No native batch — leave the cache cold; the lazy embed() path
            # fills it per-variant (still correct, just not one round trip).
            return
        now = time.monotonic()
        with self._lock:
            for t, vec in zip(want, vectors):
                if vec is not None:
                    self._put_locked(self._key(t, memory_action), vec, now)

    def _embed_many(self, texts, memory_action):
        """One ``input=[…]`` embeddings call for an OpenAI-style embedder.

        Returns a list of vectors aligned with ``texts``, or ``None`` when the
        embedder exposes no batchable client (caller then falls back to lazy
        per-variant embedding).
        """
        emb = self._embedder
        client = getattr(emb, "client", None)
        embeddings_api = getattr(client, "embeddings", None)
        create = getattr(embeddings_api, "create", None)
        cfg = getattr(emb, "config", None)
        if not callable(create) or cfg is None or not hasattr(cfg, "model"):
            return None
        try:
            kwargs = {
                "input": [(t or "").replace("\n", " ") for t in texts],
                "model": cfg.model,
                "encoding_format": "float",
            }
            if getattr(emb, "_pass_dimensions_to_api", False):
                kwargs["dimensions"] = cfg.embedding_dims
            self.batch_calls += 1
            resp = create(**kwargs)
            data = list(resp.data)
            # OpenAI returns items in input order, but each carries an explicit
            # index — sort by it when present to be bulletproof.
            if data and hasattr(data[0], "index"):
                data = sorted(data, key=lambda d: d.index)
            return [d.embedding for d in data]
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("batch embed failed (%s); per-variant fallback", type(exc).__name__)
            return None


def install(mem, ttl_s: float, maxsize: int):
    """Idempotently wrap ``mem.embedding_model.embed`` with the TTL cache."""
    emb = getattr(mem, "embedding_model", None)
    if emb is None:
        return None
    existing = getattr(emb, _CACHE_ATTR, None)
    if isinstance(existing, QueryEmbeddingCache):
        existing._ttl = float(ttl_s)
        existing._maxsize = max(1, int(maxsize))
        return existing
    try:
        cache = QueryEmbeddingCache(emb, ttl_s, maxsize)
        emb.embed = cache.embed  # instance-attribute shadow of the method
        setattr(emb, _CACHE_ATTR, cache)
        return cache
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("query embed cache install failed (%s); skipping", type(exc).__name__)
        return None


def prewarm_sync(mem, texts, ttl_s: float, maxsize: int):
    """Install (if needed) and batch-embed ``texts``. Sync; wrap in to_thread."""
    if ttl_s is None or ttl_s <= 0 or not texts:
        return None
    cache = install(mem, ttl_s, maxsize)
    if cache is None:
        return None
    cache.warm(list(texts))
    return cache

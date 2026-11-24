"""Query-embedding batch + short-TTL cache (explore-C #6 / OPT-A).

Exercises the real ``query_embed_cache`` from both packages against a fake
OpenAI-style embedder that counts ``embeddings.create`` calls, and asserts:

* N distinct variants embedded one-by-one → N create calls (baseline).
* ``warm([…])`` → exactly 1 batched create call; the subsequent per-variant
  ``embed(v, "search")`` calls hit the cache → 0 extra create calls.
* batched vectors are byte-identical to the one-by-one vectors (output-invariant).
* TTL=0 and non-"search" actions bypass the cache entirely (ingest untouched).
"""
import importlib
import os
import sys

import pytest

_VENDOR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "vendor", "mem0-alpha",
)


def _load_cache(kind):
    if kind == "services":
        return importlib.import_module("services.mem0.query_embed_cache")
    if _VENDOR not in sys.path:
        sys.path.insert(0, _VENDOR)
    try:
        return importlib.import_module("llm_mem0.query_embed_cache")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"llm_mem0 not importable: {exc}")


KINDS = ["services", "alpha"]


class _Emb:
    def __init__(self, embedding, index):
        self.embedding = embedding
        self.index = index


class _Resp:
    def __init__(self, data):
        self.data = data


class FakeEmbeddingsAPI:
    def __init__(self, parent):
        self.parent = parent

    def create(self, *, input, model, encoding_format, **kw):
        self.parent.create_calls += 1
        self.parent.total_inputs += len(input)
        # Deterministic, order-stable vector per text. Returned out of order to
        # prove the cache re-sorts by `index`.
        data = [_Emb([float(len(t)), float(sum(map(ord, t)) % 97)], i) for i, t in enumerate(input)]
        return _Resp(list(reversed(data)))


class FakeClient:
    def __init__(self, parent):
        self.embeddings = FakeEmbeddingsAPI(parent)


class FakeConfig:
    model = "text-embedding-3-small"
    embedding_dims = 1536


class FakeEmbedder:
    def __init__(self):
        self.create_calls = 0
        self.total_inputs = 0
        self.config = FakeConfig()
        self.client = FakeClient(self)
        self._pass_dimensions_to_api = False

    def embed(self, text, memory_action=None):
        text = (text or "").replace("\n", " ")
        r = self.client.embeddings.create(
            input=[text], model=self.config.model, encoding_format="float"
        )
        # one input → data has one element (ignore the reversal for n=1)
        return r.data[0].embedding


class FakeMem:
    def __init__(self, emb):
        self.embedding_model = emb


VARIANTS = ["森川あかり 一番 好き", "who do you like", "森川 あかり", "好きな人", "recall favourite"]


def test_baseline_counts_one_create_per_variant():
    emb = FakeEmbedder()
    for v in VARIANTS:
        emb.embed(v, "search")
    assert emb.create_calls == len(VARIANTS)


@pytest.mark.parametrize("kind", KINDS)
def test_prewarm_collapses_to_one_create(kind):
    mod = _load_cache(kind)
    emb = FakeEmbedder()
    mem = FakeMem(emb)

    # reference vectors (fresh embedder, one-by-one)
    ref = {v: FakeEmbedder().embed(v, "search") for v in VARIANTS}

    cache = mod.prewarm_sync(mem, VARIANTS, ttl_s=60, maxsize=256)
    assert cache is not None
    # exactly ONE batched round trip warmed all variants
    assert emb.create_calls == 1
    assert emb.total_inputs == len(VARIANTS)

    # now the per-variant search embeds all hit the cache (0 extra creates)
    for v in VARIANTS:
        vec = emb.embed(v, "search")
        assert vec == ref[v]                      # byte-identical to solo embed
    assert emb.create_calls == 1                  # still one
    assert cache.hits == len(VARIANTS)
    assert cache.batch_calls == 1


@pytest.mark.parametrize("kind", KINDS)
def test_repeated_variant_dedups_within_ttl(kind):
    mod = _load_cache(kind)
    emb = FakeEmbedder()
    mem = FakeMem(emb)
    mod.install(mem, ttl_s=60, maxsize=256)

    v = VARIANTS[0]
    a = emb.embed(v, "search")   # miss → 1 create
    b = emb.embed(v, "search")   # hit  → 0 create (refill-loop / cross-turn repeat)
    assert a == b
    assert emb.create_calls == 1


@pytest.mark.parametrize("kind", KINDS)
def test_ttl_zero_disables(kind):
    mod = _load_cache(kind)
    emb = FakeEmbedder()
    mem = FakeMem(emb)
    assert mod.prewarm_sync(mem, VARIANTS, ttl_s=0, maxsize=256) is None
    assert emb.create_calls == 0                  # nothing warmed

    cache = mod.install(mem, ttl_s=0, maxsize=256)
    emb.embed(VARIANTS[0], "search")
    emb.embed(VARIANTS[0], "search")
    assert emb.create_calls == 2                  # no caching when ttl<=0
    assert cache.passthrough == 2


@pytest.mark.parametrize("kind", KINDS)
def test_non_search_action_passthrough(kind):
    mod = _load_cache(kind)
    emb = FakeEmbedder()
    mem = FakeMem(emb)
    cache = mod.install(mem, ttl_s=60, maxsize=256)

    emb.embed("a fact to store", "add")
    emb.embed("a fact to store", "add")          # ingest path not cached
    assert emb.create_calls == 2
    assert cache.hits == 0


@pytest.mark.parametrize("kind", KINDS)
def test_ttl_expiry_recomputes(kind):
    mod = _load_cache(kind)
    emb = FakeEmbedder()
    mem = FakeMem(emb)
    cache = mod.install(mem, ttl_s=60, maxsize=256)

    import time as _t
    real = _t.monotonic
    clock = {"t": 1000.0}
    try:
        _t.monotonic = lambda: clock["t"]
        emb.embed(VARIANTS[0], "search")         # miss → create
        clock["t"] += 1.0
        emb.embed(VARIANTS[0], "search")         # still fresh → hit
        assert emb.create_calls == 1
        clock["t"] += 120.0                       # past TTL
        emb.embed(VARIANTS[0], "search")         # expired → recompute
        assert emb.create_calls == 2
    finally:
        _t.monotonic = real

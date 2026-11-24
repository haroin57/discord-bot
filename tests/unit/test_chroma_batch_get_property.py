"""Property test — Chroma batch get vs per-ID get (explore-C #4).

Mirrors the Lean model `KanaeVerify.ChromaBatchGet`: replacing the per-ID
``get`` loop with a single ``get(ids=[…])`` batch must return the same list in
the same request order, drop missing ids identically, and introduce no spurious
duplicate keys when the requested ids are distinct.
"""
import random


def per_id_get(store: dict[int, str], ids: list[int]) -> list[tuple[int, str]]:
    # Baseline N+1: one lookup per id, request order, misses dropped.
    out = []
    for i in ids:
        if i in store:
            out.append((i, store[i]))
    return out


def batch_get(store: dict[int, str], ids: list[int]) -> list[tuple[int, str]]:
    # Optimized: materialise every (id, store.get(id)) pair once, then project
    # the present ones in request order — the shape of a real batch wrapper.
    fetched = [(i, store.get(i)) for i in ids]
    return [(i, v) for (i, v) in fetched if v is not None]


def _rand_store_and_ids(rng: random.Random):
    universe = list(range(30))
    store = {k: f"fact-{k}" for k in universe if rng.random() < 0.5}
    ids = rng.sample(universe, rng.randrange(0, len(universe) + 1))  # distinct
    return store, ids


def test_batch_equals_per_id_same_order_same_set():
    rng = random.Random(20261125)
    for _ in range(3000):
        store, ids = _rand_store_and_ids(rng)
        assert batch_get(store, ids) == per_id_get(store, ids)


def test_no_extra_duplicate_keys():
    rng = random.Random(7)
    for _ in range(3000):
        store, ids = _rand_store_and_ids(rng)
        keys = [k for (k, _) in batch_get(store, ids)]
        assert len(keys) == len(set(keys))          # Nodup
        assert set(keys) <= set(ids)                # no key outside the request

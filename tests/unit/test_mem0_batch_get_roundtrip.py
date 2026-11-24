"""Chroma batch get — round-trip count + per-ID equivalence (explore-C #4 / OPT-A).

The production helper ``batch_get_facts_sync`` replaces the N+1 ``mem.get(id)``
fan-out (search.py hydrate / graph sibling fetch) with a single Chroma
``collection.get(ids=[…])`` round trip. These tests exercise the *real*
production helper (from both the alpha-vendored ``llm_mem0.client`` and the
non-alpha ``services.mem0.client``) against a fake Chroma collection that counts
``get`` calls, and assert the Lean ``KanaeVerify.ChromaBatchGet`` invariants in
Python:

* N per-ID round trips → 1 batch round trip.
* batch == per-ID: same set, request order preserved by the caller's projection,
  missing ids dropped identically, no spurious duplicate keys.
* per-record dict is byte-identical to the mem0 ``Memory.get`` contract.

Order-independence is proved by having the fake return present ids in *reversed*
order: callers look up the id→fact map in their own request order, so Chroma's
internal get ordering cannot affect the result.
"""
import importlib
import os
import random
import sys

import pytest

_VENDOR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "vendor", "mem0-alpha",
)


def _load_client(kind):
    if kind == "services":
        return importlib.import_module("services.mem0.client")
    if _VENDOR not in sys.path:
        sys.path.insert(0, _VENDOR)
    try:
        return importlib.import_module("llm_mem0.client")
    except Exception as exc:  # pragma: no cover - vendored pkg absent
        pytest.skip(f"llm_mem0 not importable: {exc}")


CLIENTS = ["services", "alpha"]


class FakeChromaCollection:
    """Mimics chromadb Collection.get(ids=[…]); counts calls, returns reversed
    present-id order to prove the result is independent of Chroma's ordering."""

    def __init__(self, store):
        self._store = store  # {id: payload_dict}
        self.get_calls = 0
        self.last_ids = None

    def get(self, ids=None, where=None, limit=None, include=None):
        self.get_calls += 1
        self.last_ids = list(ids) if ids is not None else None
        present = [i for i in (ids or []) if i in self._store]
        present = list(reversed(present))  # deliberately NOT request order
        return {
            "ids": present,
            "metadatas": [self._store[i] for i in present],
            "documents": [self._store[i].get("data", "") for i in present],
        }


class FakeVectorStore:
    def __init__(self, coll):
        self.collection = coll


class FakeMem:
    def __init__(self, client_mod, store):
        self._store = store
        self._format = client_mod._format_vector_record
        self.vector_store = FakeVectorStore(FakeChromaCollection(store))
        self.get_calls = 0

    def get(self, memory_id):
        # Mirrors mem0 Memory.get: shaped dict for present, None for missing.
        self.get_calls += 1
        payload = self._store.get(memory_id)
        if payload is None:
            return None
        return self._format(memory_id, payload)


def _per_id_list(mem, ids):
    out = []
    for i in ids:
        fact = mem.get(i)
        if fact:
            out.append(fact)
    return out


def _rand_store_and_ids(rng):
    universe = [f"m{n}" for n in range(30)]
    store = {
        k: {"data": f"fact {k}", "hash": f"h{k}", "user_id": "u1", "topic": k}
        for k in universe
        if rng.random() < 0.5
    }
    ids = rng.sample(universe, rng.randrange(0, len(universe) + 1))  # distinct
    return store, ids


@pytest.mark.parametrize("kind", CLIENTS)
def test_single_round_trip(kind):
    client = _load_client(kind)
    store = {f"m{n}": {"data": f"d{n}", "user_id": "u1"} for n in range(10)}
    mem = FakeMem(client, store)
    ids = [f"m{n}" for n in range(10)]

    got = client.batch_get_facts_sync(mem, ids)

    # One batch call for 10 ids (vs 10 per-id gets).
    assert mem.vector_store.collection.get_calls == 1
    assert mem.get_calls == 0
    assert set(got) == set(ids)


@pytest.mark.parametrize("kind", CLIENTS)
def test_batch_equals_per_id_projection(kind):
    client = _load_client(kind)
    rng = random.Random(20261125)
    for _ in range(2000):
        store, ids = _rand_store_and_ids(rng)
        mem_batch = FakeMem(client, store)
        mem_peri = FakeMem(client, store)

        facts_by_id = client.batch_get_facts_sync(mem_batch, ids)
        # Caller-side projection in request order (how hydrate/fetch_sibling use it).
        batch_projection = [facts_by_id[i] for i in ids if i in facts_by_id]
        per_id = _per_id_list(mem_peri, ids)

        assert batch_projection == per_id            # same set + same order
        # missing ids dropped identically
        assert set(facts_by_id) == {i for i in ids if i in store}
        # no key outside the (distinct) request, no dup keys
        assert set(facts_by_id) <= set(ids)
        # exactly one round trip regardless of N (or zero when nothing requested)
        assert mem_batch.vector_store.collection.get_calls == (1 if ids else 0)


@pytest.mark.parametrize("kind", CLIENTS)
def test_dedup_and_empty(kind):
    client = _load_client(kind)
    store = {"a": {"data": "A", "user_id": "u"}, "b": {"data": "B", "user_id": "u"}}
    mem = FakeMem(client, store)
    # duplicate + missing ids in the request
    got = client.batch_get_facts_sync(mem, ["a", "a", "b", "missing", "b"])
    assert set(got) == {"a", "b"}
    assert mem.vector_store.collection.get_calls == 1
    # the de-duplicated request is what reaches Chroma
    assert mem.vector_store.collection.last_ids == ["a", "b", "missing"]

    empty = client.batch_get_facts_sync(mem, [])
    assert empty == {}


@pytest.mark.parametrize("kind", CLIENTS)
def test_record_shape_matches_memory_get_contract(kind):
    """`_format_vector_record` must reproduce mem0 Memory.get's dict exactly."""
    client = _load_client(kind)
    payload = {
        "data": "先輩が好き",
        "hash": "abc",
        "user_id": "573489206240870410",
        "created_at": None,
        "updated_at": None,
        "topic": "feelings",
        "scope": "global",
    }
    out = client._format_vector_record("m42", payload)
    assert out == {
        "id": "m42",
        "memory": "先輩が好き",
        "hash": "abc",
        "metadata": {"topic": "feelings", "scope": "global"},
        "score": None,
        "created_at": None,
        "updated_at": None,
        "user_id": "573489206240870410",
    }
    # No extra metadata → metadata key stays None (model_dump default), matching
    # Memory.get (which only overwrites it when additional_metadata is truthy).
    bare = client._format_vector_record("m1", {"data": "x", "hash": "h"})
    assert bare["metadata"] is None
    assert bare["memory"] == "x" and bare["score"] is None


@pytest.mark.parametrize("kind", CLIENTS)
def test_falls_back_to_per_id_without_collection(kind):
    client = _load_client(kind)

    class NoCollMem:
        def __init__(self, store):
            self._store = store
            self.vector_store = None
            self.get_calls = 0

        def get(self, mid):
            self.get_calls += 1
            payload = self._store.get(mid)
            return client._format_vector_record(mid, payload) if payload else None

    store = {"a": {"data": "A"}, "b": {"data": "B"}}
    mem = NoCollMem(store)
    got = client.batch_get_facts_sync(mem, ["a", "missing", "b"])
    assert set(got) == {"a", "b"}
    assert mem.get_calls == 3  # fell back to per-id

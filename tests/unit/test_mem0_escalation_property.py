"""Property test — Mem0 cheap-first escalation (explore-C #2).

Mirrors the Lean model `KanaeVerify.Escalation` (lean/KanaeVerify): the
cheap-first optimization must (1) decide escalation as a deterministic pure
function of cheap confidence, (2) return exactly the reference recall set, and
(3) skip the expensive tier when confident. Randomized (property-style) so the
invariant holds across the whole input space, documenting the contract the Phase
C overlay cheap-first pipeline must satisfy.
"""
import random

THRESHOLD = 50


def escalate(confidence: int) -> bool:
    return confidence < THRESHOLD


def spec(hits: list[int], confidence: int, extra: list[int]) -> list[int]:
    # Reference: branch the whole result.
    return hits + extra if escalate(confidence) else hits


def optimized(hits: list[int], confidence: int, extra: list[int]) -> list[int]:
    # Cheap-first: always keep cheap hits, append the expensive extras only on
    # escalation (structurally different from `spec`, provably equal in Lean).
    return hits + (extra if escalate(confidence) else [])


def _rand_case(rng: random.Random):
    hits = [rng.randrange(1000) for _ in range(rng.randrange(0, 8))]
    extra = [rng.randrange(1000) for _ in range(rng.randrange(0, 8))]
    confidence = rng.randrange(0, 100)
    return hits, confidence, extra


def test_optimized_equals_spec():
    rng = random.Random(20261125)
    for _ in range(2000):
        hits, confidence, extra = _rand_case(rng)
        assert optimized(hits, confidence, extra) == spec(hits, confidence, extra)


def test_escalate_is_deterministic():
    rng = random.Random(1)
    for _ in range(2000):
        c = rng.randrange(0, 100)
        assert escalate(c) == escalate(c)


def test_skips_expensive_tier_when_confident():
    rng = random.Random(2)
    for _ in range(2000):
        hits, _, extra = _rand_case(rng)
        confidence = rng.randrange(THRESHOLD, 100)  # confident ⇒ no escalation
        assert optimized(hits, confidence, extra) == hits


# ---------------------------------------------------------------------------
# Real-implementation property tests — exercise services.mem0.search's
# cheap-first orchestration end-to-end with a fake retriever (no LLM, no I/O).
#
# These bind the abstract Lean model above to the actual Python:
#   * flag OFF  → byte-identical call sequence to the unconditional pipeline
#                 (all three helper LLMs + rerank fire every turn).
#   * flag ON + confident cheap read → helper LLMs + rerank SKIPPED.
#   * flag ON + weak cheap read → escalate; helpers fire and the result set
#     equals the full flag-off result (convergence / union preserved).
# ---------------------------------------------------------------------------

import random as _random

import pytest

_PATCH_BASE = "services.mem0.search"

# Distances: CONFIDENT is below the 0.75 escalate threshold (skip helpers);
# WEAK is at/above it (escalate). The expensive helper variants surface a
# second, "extra" memory the cheap tier alone never retrieves.
_CHEAP_MEM = {"id": "cheap", "memory": "cheap fact", "score": None}
_EXTRA_MEM = {"id": "extra", "memory": "helper-only fact", "score": None}


class _Recorder:
    """Counts helper-LLM / rerank invocations and the query sets seen."""

    def __init__(self, cheap_distance: float):
        self.cheap_distance = cheap_distance
        self.rewrite = 0
        self.hyde = 0
        self.keywords = 0
        self.rerank = 0
        self.multi_query_sets: list[list[str]] = []

    def install(self, monkeypatch):
        async def _rewrite(query):
            self.rewrite += 1
            return ["rewrite-variant"]

        async def _hyde(query):
            self.hyde += 1
            return "hyde-variant"

        async def _keywords(query):
            self.keywords += 1
            return "keyword variant"

        async def _rerank(query, candidates, top_k):
            self.rerank += 1
            return list(candidates)[:top_k]

        async def _multi(queries, user_id, *, limit, apply_distance_filter,
                         current_channel_id=None):
            self.multi_query_sets.append(list(queries))
            # Helper variants (rewrite/HyDE/keywords) are what surface the
            # "extra" memory; the cheap query set (original + alias + morpho)
            # only ever retrieves the cheap hit.
            expensive = any(
                q in ("rewrite-variant", "hyde-variant", "keyword variant")
                for q in queries
            )
            cheap = dict(_CHEAP_MEM, score=self.cheap_distance)
            if expensive:
                return [cheap, dict(_EXTRA_MEM, score=self.cheap_distance)]
            return [cheap]

        async def _graph_identity(candidates, user_id, *, current_channel_id=None):
            return candidates

        monkeypatch.setattr(f"{_PATCH_BASE}._expand_query_via_graph",
                            _async_const([]))
        monkeypatch.setattr(f"{_PATCH_BASE}._expand_query_via_morpho",
                            lambda q: "")
        monkeypatch.setattr(f"{_PATCH_BASE}._prewarm_query_embeddings",
                            _async_const(None))
        monkeypatch.setattr(f"{_PATCH_BASE}._rewrite_query", _rewrite)
        monkeypatch.setattr(f"{_PATCH_BASE}._generate_hypothetical_fact", _hyde)
        monkeypatch.setattr(f"{_PATCH_BASE}._extract_keywords_via_llm", _keywords)
        monkeypatch.setattr(f"{_PATCH_BASE}._rerank_memories", _rerank)
        monkeypatch.setattr(f"{_PATCH_BASE}._search_multi_hybrid", _multi)
        monkeypatch.setattr(f"{_PATCH_BASE}._graph_expand_candidates",
                            _graph_identity)
        monkeypatch.setattr(f"{_PATCH_BASE}._apply_reinforcement_boost",
                            lambda c: None)
        monkeypatch.setattr(f"{_PATCH_BASE}._fire_recall_reinforcement",
                            lambda c: None)
        monkeypatch.setattr(f"{_PATCH_BASE}._expand_episode_bundle",
                            _async_const(0))


def _async_const(value):
    async def _f(*a, **k):
        return value
    return _f


def _set_flag(monkeypatch, enabled: bool):
    import config
    monkeypatch.setattr(config, "MEM0_CHEAP_FIRST_ENABLED", enabled)


def _ids(result):
    return [r.get("id") for r in result if r.get("id")]


@pytest.mark.asyncio
async def test_flag_off_always_runs_full_pipeline(monkeypatch):
    """Flag OFF: every helper LLM + rerank fires regardless of cheap
    confidence (byte-identical to the unconditional pipeline)."""
    from services.mem0.search import search_memories_smart

    for dist in (0.1, 0.5, 0.9, 1.3):  # confident .. no_memory
        rec = _Recorder(cheap_distance=dist)
        rec.install(monkeypatch)
        _set_flag(monkeypatch, False)
        result = await search_memories_smart("先輩の投資戦略", "u", limit=5)
        assert (rec.rewrite, rec.hyde, rec.keywords, rec.rerank) == (1, 1, 1, 1)
        # Full query set includes helper variants, so the extra memory surfaces.
        assert "extra" in _ids(result)


@pytest.mark.asyncio
async def test_flag_on_confident_skips_helpers(monkeypatch):
    """Flag ON + confident cheap read: helper trio + rerank are all skipped
    and only the cheap hit is returned."""
    from services.mem0.search import search_memories_smart

    rec = _Recorder(cheap_distance=0.2)  # < 0.75 escalate threshold
    rec.install(monkeypatch)
    _set_flag(monkeypatch, True)
    result = await search_memories_smart("先輩の投資戦略", "u", limit=5)
    assert (rec.rewrite, rec.hyde, rec.keywords, rec.rerank) == (0, 0, 0, 0)
    assert _ids(result) == ["cheap"]
    # The cheap query set never carried a helper variant.
    assert all(
        "rewrite-variant" not in qs for qs in rec.multi_query_sets
    )


@pytest.mark.asyncio
async def test_flag_on_weak_escalates_to_full(monkeypatch):
    """Flag ON + weak cheap read: escalate; helpers fire and the result set
    converges on the flag-off (full) result."""
    from services.mem0.search import search_memories_smart

    # OFF reference result for the same weak query.
    rec_off = _Recorder(cheap_distance=0.9)
    rec_off.install(monkeypatch)
    _set_flag(monkeypatch, False)
    off = await search_memories_smart("先輩の投資戦略", "u", limit=5)

    rec_on = _Recorder(cheap_distance=0.9)  # >= 0.75 → escalate
    rec_on.install(monkeypatch)
    _set_flag(monkeypatch, True)
    on = await search_memories_smart("先輩の投資戦略", "u", limit=5)

    # Helpers fired on escalation, and the result set matches OFF exactly.
    assert (rec_on.rewrite, rec_on.hyde, rec_on.keywords, rec_on.rerank) == (1, 1, 1, 1)
    assert set(_ids(on)) == set(_ids(off))
    assert "extra" in _ids(on)


@pytest.mark.asyncio
async def test_cheap_first_property_over_random_confidence(monkeypatch):
    """Property: across random cheap distances, ON converges on the reference
    cheap-first semantics — confident ⇒ cheap-only (helpers skipped), weak ⇒
    full set (helpers fired)."""
    from services.mem0.search import search_memories_smart, should_escalate_recall

    rng = _random.Random(20261125)
    for _ in range(200):
        dist = round(rng.uniform(0.0, 1.4), 3)
        escalate = should_escalate_recall({"best_distance": dist})

        rec = _Recorder(cheap_distance=dist)
        rec.install(monkeypatch)
        _set_flag(monkeypatch, True)
        result = await search_memories_smart("先輩の投資戦略", "u", limit=5)

        helpers_fired = (rec.rewrite, rec.hyde, rec.keywords, rec.rerank)
        if escalate:
            assert helpers_fired == (1, 1, 1, 1)
            assert "extra" in _ids(result)
        else:
            assert helpers_fired == (0, 0, 0, 0)
            assert _ids(result) == ["cheap"]


@pytest.mark.asyncio
async def test_both_disabled_is_noop_for_cheap_first(monkeypatch):
    """When the caller already asked for the plain path (rewrite=rerank=False),
    cheap-first is a no-op: flag ON must not change behaviour vs flag OFF."""
    from services.mem0.search import search_memories_smart

    rec_off = _Recorder(cheap_distance=0.2)
    rec_off.install(monkeypatch)
    _set_flag(monkeypatch, False)
    off = await search_memories_smart("q", "u", limit=5, rewrite=False, rerank=False)

    rec_on = _Recorder(cheap_distance=0.2)
    rec_on.install(monkeypatch)
    _set_flag(monkeypatch, True)
    on = await search_memories_smart("q", "u", limit=5, rewrite=False, rerank=False)

    assert _ids(off) == _ids(on) == ["cheap"]
    assert (rec_on.rewrite, rec_on.hyde, rec_on.keywords, rec_on.rerank) == (0, 0, 0, 0)

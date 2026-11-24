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

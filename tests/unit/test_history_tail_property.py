"""Property test — history tail selection + token budget (explore-C / session.py).

Mirrors the Lean model `KanaeVerify.HistoryTail`: the reverse-scan tail
optimization must return exactly the last ``n`` rows in chronological order
(equal to the reference slice), never more than ``n`` rows, and the token-budget
variant must never exceed the budget.
"""
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    id: int
    tokens: int


def spec(rows: list[Row], n: int) -> list[Row]:
    # Reference: last n rows, chronological order (= (reverse.take n).reverse).
    return rows[max(0, len(rows) - n):]


def optimized(rows: list[Row], n: int) -> list[Row]:
    # Reverse-scan: collect from the tail until n, then restore order.
    out: list[Row] = []
    for r in reversed(rows):
        if len(out) >= n:
            break
        out.append(r)
    out.reverse()
    return out


def within_budget(rows: list[Row], budget: int) -> list[Row]:
    # Greedy newest-first keep while cumulative tokens fit, restored to order.
    out: list[Row] = []
    rem = budget
    for r in reversed(rows):
        if r.tokens <= rem:
            out.append(r)
            rem -= r.tokens
        else:
            break
    out.reverse()
    return out


def _rand_rows(rng: random.Random) -> list[Row]:
    return [Row(id=i, tokens=rng.randrange(0, 30)) for i in range(rng.randrange(0, 40))]


def test_optimized_equals_spec_and_preserves_order():
    rng = random.Random(20261125)
    for _ in range(3000):
        rows = _rand_rows(rng)
        n = rng.randrange(0, len(rows) + 5)
        assert optimized(rows, n) == spec(rows, n)


def test_optimized_len_le_n():
    rng = random.Random(3)
    for _ in range(3000):
        rows = _rand_rows(rng)
        n = rng.randrange(0, len(rows) + 5)
        assert len(optimized(rows, n)) <= n


def test_within_budget_never_exceeds_budget():
    rng = random.Random(11)
    for _ in range(3000):
        rows = _rand_rows(rng)
        budget = rng.randrange(0, 200)
        kept = within_budget(rows, budget)
        assert sum(r.tokens for r in kept) <= budget
        # kept is a contiguous chronological suffix of rows
        assert kept == rows[len(rows) - len(kept):]

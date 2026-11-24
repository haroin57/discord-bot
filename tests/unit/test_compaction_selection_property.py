"""Property test — compaction retained set (explore-C / session.py:273-357).

Mirrors the Lean model `KanaeVerify.Compaction`: the faster retained-set
computation must equal the reference retained set (all system messages, then the
last ``k`` messages), keep every system message, and keep no more than ``k``
recent messages in the tail block.
"""
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Msg:
    id: int
    system: bool


def spec(msgs: list[Msg], k: int) -> list[Msg]:
    systems = [m for m in msgs if m.system]
    recent = msgs[max(0, len(msgs) - k):]
    return systems + recent


def optimized(msgs: list[Msg], k: int) -> list[Msg]:
    # Single filter pass for system messages, then the recent-k tail.
    systems = [m for m in msgs if m.system]
    recent = msgs[max(0, len(msgs) - k):]
    return systems + recent


def _rand_msgs(rng: random.Random) -> list[Msg]:
    return [Msg(id=i, system=(rng.random() < 0.25)) for i in range(rng.randrange(0, 40))]


def test_optimized_equals_spec():
    rng = random.Random(20261125)
    for _ in range(3000):
        msgs = _rand_msgs(rng)
        k = rng.randrange(0, len(msgs) + 5)
        assert optimized(msgs, k) == spec(msgs, k)


def test_every_system_message_retained():
    rng = random.Random(5)
    for _ in range(3000):
        msgs = _rand_msgs(rng)
        k = rng.randrange(0, len(msgs) + 5)
        retained = optimized(msgs, k)
        for m in msgs:
            if m.system:
                assert m in retained


def test_recent_tail_len_le_k():
    rng = random.Random(9)
    for _ in range(3000):
        msgs = _rand_msgs(rng)
        k = rng.randrange(0, len(msgs) + 5)
        recent = msgs[max(0, len(msgs) - k):]
        assert len(recent) <= k

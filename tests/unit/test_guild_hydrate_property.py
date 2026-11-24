"""Property test — guild_context hydrate order-independence (explore-C #7).

Two layers, both mirroring the Lean model `KanaeVerify.GuildHydrate`
(`hydrate_order_independent` / `optimized_eq_spec`):

1. pure model: the visible id *set* of `flatten(channels)` is invariant under
   any permutation of the channel list.
2. real code: the concurrent `_hydrate_visible` (Batch 2 optimization — hydrate
   the eligible channels with `asyncio.gather` instead of serially) must produce
   the same final visible id set regardless of the channel sort/hydration order
   and across repeated (nondeterministic) concurrent runs.
"""
import asyncio
from datetime import datetime, timezone
import random
from types import SimpleNamespace as NS

import pytest

from services import guild_context as gc


# ---- layer 1: pure flatten model --------------------------------------------

def hydrate(channels: list[list[int]]) -> list[int]:
    return [x for ch in channels for x in ch]


def test_visible_set_is_permutation_invariant():
    rng = random.Random(20261125)
    for _ in range(3000):
        channels = [[rng.randrange(100) for _ in range(rng.randrange(0, 5))]
                    for _ in range(rng.randrange(0, 6))]
        shuffled = channels[:]
        rng.shuffle(shuffled)
        assert set(hydrate(channels)) == set(hydrate(shuffled))


# ---- layer 2: real concurrent _hydrate_visible ------------------------------

def _make_channel(guild, cid, last_mid, items):
    async def history(limit):
        for item in items:
            yield item
    return NS(id=cid, guild=guild, last_message_id=last_mid,
              permissions_for=lambda member: NS(view_channel=True, read_message_history=True),
              history=history)


def _make_item(guild, channel, mid, uid=30):
    return NS(id=mid, guild=guild, channel=channel,
              author=NS(id=uid, display_name=f'user-{uid}', bot=False),
              content=f'msg-{mid}', created_at=datetime.now(timezone.utc), edited_at=None)


@pytest.mark.asyncio
async def test_real_hydrate_final_visible_set_is_order_independent(tmp_path, monkeypatch):
    monkeypatch.setenv('GUILD_CONTEXT_ENABLED', 'true')
    rng = random.Random(20261125)
    for trial in range(40):
        n_channels = rng.randrange(1, 7)          # ≤ 6 so all are hydrated
        channel_ids = rng.sample(range(100, 100 + 50), n_channels)
        # Fixed logical content for this trial: same message ids every run, so
        # only the channel sort/hydration order varies across runs.
        slots_per = {cid: rng.randrange(1, 4) for cid in channel_ids}
        expected_ids = {cid * 100 + slot
                        for cid in channel_ids for slot in range(slots_per[cid])}
        baseline: set[int] | None = None
        for run in range(3):
            store = gc.GuildContextStore(tmp_path / f'g-{trial}-{run}.sqlite')
            monkeypatch.setattr(gc, 'get_store', lambda s=store: s)
            monkeypatch.setattr(gc, '_hydrated', {})
            monkeypatch.setattr(gc, '_hydrate_locks', {})
            monkeypatch.setattr(gc, '_store_lock', asyncio.Lock())
            guild = NS(id=10, text_channels=[], threads=[])
            channels = []
            for cid in channel_ids:
                items = []
                # random last_message_id ⇒ different internal sort/hydration order
                ch = _make_channel(guild, cid, rng.randrange(1, 10_000), items)
                for slot in range(slots_per[cid]):
                    items.append(_make_item(guild, ch, cid * 100 + slot))
                channels.append(ch)
            guild.text_channels = channels
            msg = NS(id=999999, guild=guild, channel=channels[0], author=NS(id=30))
            await gc._hydrate_visible(msg)
            visible = {row['message_id'] for row in store.recent(10)}
            if baseline is None:
                baseline = visible
            else:
                assert visible == baseline          # order/concurrency-independent
        # the hydrated visible set is exactly the union of all channel messages
        assert baseline == expected_ids

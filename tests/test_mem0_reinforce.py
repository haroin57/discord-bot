"""Tests for services.mem0.reinforce — Hebbian dedup increments."""

from __future__ import annotations

import math
import time
from unittest.mock import MagicMock

import pytest

from services.mem0.reinforce import (
    MAX_IMPORTANCE,
    MAX_REINFORCED_HISTORY,
    reinforce_existing_sync,
    reinforcement_boost,
)


class TestReinforceExistingSync:
    def test_first_reinforce_increments_to_two(self):
        mem = MagicMock()
        mem.get.return_value = {
            "id": "m1",
            "memory": "先輩は Rust が好き",
            "metadata": {"importance": 3, "category": "tech"},
        }
        ok = reinforce_existing_sync(mem, "m1")
        assert ok is True
        mem.update.assert_called_once()
        kwargs = mem.update.call_args.kwargs
        partial = kwargs["metadata"]
        assert kwargs["memory_id"] == "m1"
        assert kwargs["data"] == "先輩は Rust が好き"
        # Partial update — only changed keys are sent
        assert partial["mention_count"] == 2
        assert partial["importance"] == 4
        assert len(partial["reinforced_at"]) == 1
        assert partial["last_reinforced_at"] == partial["reinforced_at"][0]
        assert "category" not in partial  # preserved server-side

    def test_subsequent_reinforce(self):
        mem = MagicMock()
        mem.get.return_value = {
            "id": "m1",
            "memory": "fact text",
            "metadata": {
                "mention_count": 5,
                "importance": 4,
                "reinforced_at": [100, 200, 300],
                "last_reinforced_at": 300,
            },
        }
        reinforce_existing_sync(mem, "m1")
        partial = mem.update.call_args.kwargs["metadata"]
        assert partial["mention_count"] == 6
        assert partial["importance"] == 5  # bumped
        assert len(partial["reinforced_at"]) == 4
        assert partial["last_reinforced_at"] > 300

    def test_importance_caps_at_five(self):
        mem = MagicMock()
        mem.get.return_value = {
            "id": "m1", "memory": "x", "metadata": {"importance": 5},
        }
        reinforce_existing_sync(mem, "m1")
        assert mem.update.call_args.kwargs["metadata"]["importance"] == MAX_IMPORTANCE

    def test_reinforced_at_trims_to_max_history(self):
        existing = list(range(20))  # 20 entries already
        mem = MagicMock()
        mem.get.return_value = {
            "id": "m1",
            "memory": "x",
            "metadata": {"mention_count": 21, "reinforced_at": existing},
        }
        reinforce_existing_sync(mem, "m1")
        partial = mem.update.call_args.kwargs["metadata"]
        assert len(partial["reinforced_at"]) == MAX_REINFORCED_HISTORY
        assert partial["reinforced_at"][-1] == partial["last_reinforced_at"]

    def test_empty_memory_id_returns_false(self):
        mem = MagicMock()
        assert reinforce_existing_sync(mem, "") is False
        mem.update.assert_not_called()

    def test_get_raising_is_fail_open(self):
        mem = MagicMock()
        mem.get.side_effect = RuntimeError("backend down")
        assert reinforce_existing_sync(mem, "m1") is False
        mem.update.assert_not_called()

    def test_update_raising_is_fail_open(self):
        mem = MagicMock()
        mem.get.return_value = {"id": "m1", "memory": "x", "metadata": {}}
        mem.update.side_effect = RuntimeError("write failed")
        assert reinforce_existing_sync(mem, "m1") is False

    def test_missing_memory_text_returns_false(self):
        # Without memory text we can't re-pass `data` to mem.update.
        mem = MagicMock()
        mem.get.return_value = {"id": "m1", "metadata": {}}
        assert reinforce_existing_sync(mem, "m1") is False
        mem.update.assert_not_called()


class TestReinforcementBoost:
    def test_none_metadata_returns_zero(self):
        assert reinforcement_boost(None) == 0.0

    def test_default_mention_one_no_freq_boost(self):
        # mention_count=1 → log(1 + α*0) = 0
        boost = reinforcement_boost({"mention_count": 1})
        assert boost == 0.0

    def test_higher_mention_means_higher_boost(self):
        low = reinforcement_boost({"mention_count": 2})
        high = reinforcement_boost({"mention_count": 20})
        assert high > low > 0.0

    def test_log_dampening(self):
        # 20 mentions shouldn't be 10x 2 mentions — log dampens
        b2 = reinforcement_boost({"mention_count": 2})
        b20 = reinforcement_boost({"mention_count": 20})
        assert b20 < b2 * 10

    def test_recency_decays(self):
        now = int(time.time())
        recent = reinforcement_boost({
            "mention_count": 5, "last_reinforced_at": now - 60,  # 1 minute ago
        })
        old = reinforcement_boost({
            "mention_count": 5, "last_reinforced_at": now - 365 * 86400,  # 1 year ago
        })
        assert recent > old

    def test_malformed_metadata_falls_back(self):
        # mention_count is a string → default 1
        b = reinforcement_boost({"mention_count": "bogus"})
        assert b == 0.0

    # ----- Phase A-2: temporal age decay -----

    def test_age_decay_recent_fact_has_higher_boost(self):
        now = int(time.time())
        recent = reinforcement_boost({"timestamp": now - 60})
        old = reinforcement_boost({"timestamp": now - 365 * 86400})
        # Both should be > 0 but recent should outweigh old.
        assert recent > old > 0.0

    def test_age_decay_zero_when_no_timestamp(self):
        # No timestamp / created_at_unix → age component disabled.
        b = reinforcement_boost({"mention_count": 1})
        assert b == 0.0

    def test_age_decay_uses_created_at_unix_when_present(self):
        now = int(time.time())
        # created_at_unix takes precedence over timestamp.
        b1 = reinforcement_boost({"created_at_unix": now - 1})
        b2 = reinforcement_boost({"timestamp": now - 1})
        assert pytest.approx(b1, abs=1e-6) == b2

    def test_age_decay_can_be_disabled_via_argument(self):
        now = int(time.time())
        # alpha_age=0 disables the age component, leaving only mention/recency.
        b_off = reinforcement_boost(
            {"timestamp": now - 60, "mention_count": 1}, alpha_age=0.0,
        )
        assert b_off == 0.0

    def test_combined_boost_stays_within_reasonable_range(self):
        # Heavy fact: 100 mentions, just reinforced, brand new.
        now = int(time.time())
        b = reinforcement_boost({
            "mention_count": 100,
            "last_reinforced_at": now,
            "timestamp": now,
        })
        # Loose upper bound — three components, each capped by α coefficient.
        # mention ≈ 0.10 * log(100) ≈ 0.46
        # recency ≈ 0.05 * 1.0 = 0.05
        # age ≈ 0.03 * 1.0 = 0.03
        # → 0.54 ish. Asserting < 1.0 prevents future regressions where
        # someone bumps α coefficients without thinking about totals.
        assert 0.3 < b < 1.0


class TestDedupSignatureChange:
    """Smoke that check_near_dup returns the verdict tuple incl. existing_id."""

    @pytest.mark.asyncio
    async def test_hard_drop_returns_existing_id(self, monkeypatch):
        from services.mem0 import dedup
        # score is *distance* (small = similar). Hard drop when distance is
        # small enough that the texts are effectively identical.
        is_dup, reason, existing_id, _conflict, _merged = await dedup.check_near_dup(
            "candidate text",
            "tech",
            [{"id": "existing-1", "memory": "duplicate", "score": 0.05,
              "metadata": {"category": "tech"}}],
        )
        assert is_dup is True
        assert "hard_drop" in reason
        assert existing_id == "existing-1"

    @pytest.mark.asyncio
    async def test_novel_returns_none(self):
        from services.mem0 import dedup
        # Large distance = unrelated = keep
        is_dup, reason, existing_id, _conflict, _merged = await dedup.check_near_dup(
            "candidate text",
            "tech",
            [{"id": "x", "memory": "different", "score": 1.20}],
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_category_mismatch_skips(self):
        from services.mem0 import dedup
        # Even at tiny distance (very similar text), cross-category mismatch
        # must be skipped by the category guard.
        is_dup, reason, existing_id, _conflict, _merged = await dedup.check_near_dup(
            "candidate text",
            "tech",
            [{"id": "x", "memory": "near-identical but different category",
              "score": 0.02, "metadata": {"category": "hobby"}}],
        )
        assert is_dup is False
        assert existing_id is None

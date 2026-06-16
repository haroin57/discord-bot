"""Unit tests for services/mem0/dedup.py — all external I/O is mocked."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import services.mem0.dedup as dedup_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    score: float,
    text: str = "some memory",
    mid: str = "mem-id-1",
    category: str | None = None,
) -> dict:
    result: dict = {"id": mid, "memory": text, "score": score}
    if category is not None:
        result["metadata"] = {"category": category}
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_relation_paraphrase(monkeypatch):
    async def _judge(existing, candidate):
        return "paraphrase"
    monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _judge)


@pytest.fixture
def mock_relation_different(monkeypatch):
    async def _judge(existing, candidate):
        return "different"
    monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _judge)


@pytest.fixture
def mock_relation_mergeable(monkeypatch):
    async def _judge(existing, candidate):
        return "mergeable"
    monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _judge)
    async def _synth(existing, candidate):
        return "MERGED: " + candidate
    monkeypatch.setattr("services.mem0.dedup.synthesize_facts", _synth)


# Back-compat aliases — the old binary fixtures mapped onto the 3-way judge.
@pytest.fixture
def mock_llm_judge_true(mock_relation_paraphrase):
    """Old 'same' verdict ≈ paraphrase (drop the candidate)."""
    yield


@pytest.fixture
def mock_llm_judge_false(mock_relation_different):
    """Old 'different' verdict (keep the candidate)."""
    yield


# ---------------------------------------------------------------------------
# check_near_dup tests
# ---------------------------------------------------------------------------

class TestCheckNearDup:
    @pytest.mark.asyncio
    async def test_check_near_dup_hard_drop(self, mock_llm_judge_false):
        """score=0.10 is within hard-drop threshold → (True, hard_drop…, id)."""
        result = _make_result(score=0.10, mid="existing-id")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")
        assert existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_check_near_dup_novel(self, mock_llm_judge_false):
        """score=0.50 is above gray-zone threshold → (False, 'novel', None)."""
        result = _make_result(score=0.50, mid="existing-id")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_check_near_dup_gray_zone_same(self, mock_llm_judge_true):
        """score=0.25 (gray zone), LLM says same → (True, llm_judged_same…, id)."""
        result = _make_result(score=0.25, mid="existing-id")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is True
        assert reason.startswith("paraphrase")
        assert existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_check_near_dup_gray_zone_different(self, mock_llm_judge_false):
        """score=0.25 (gray zone), LLM says different → (False, 'novel', None)."""
        result = _make_result(score=0.25, mid="existing-id")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_check_near_dup_empty_results(self):
        """Empty near_results list → (False, 'novel', None)."""
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, []
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_check_near_dup_category_mismatch_skipped(self):
        """Different-category memory is skipped even at hard-drop distance."""
        result = _make_result(score=0.05, mid="existing-id", category="personal")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", "tech", [result]
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_check_near_dup_same_category_not_skipped(self):
        """Same category does not skip dedup — hard_drop fires normally."""
        result = _make_result(score=0.05, mid="existing-id", category="personal")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", "personal", [result]
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")
        assert existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_check_near_dup_no_candidate_category_does_not_skip(self):
        """candidate_category=None means no category filter; hard_drop fires."""
        result = _make_result(score=0.05, mid="existing-id", category="personal")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")

    @pytest.mark.asyncio
    async def test_check_near_dup_no_existing_category_does_not_skip(self):
        """existing_category missing → category filter does not skip."""
        result = _make_result(score=0.05, mid="existing-id")  # no metadata
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", "tech", [result]
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")

    # ----- Phase A-3: dry_run logging -----

    @pytest.mark.asyncio
    async def test_dry_run_does_not_drop_on_hard_drop(self, mock_llm_judge_false):
        """dry_run=True → hard_drop distance returns is_dup=False with prefix."""
        result = _make_result(score=0.05, mid="existing-id")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, [result], dry_run=True,
        )
        assert is_dup is False
        assert reason.startswith("dry_run_would_hard_drop")
        # existing_id still returned for downstream reinforcement metrics
        assert existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_dry_run_does_not_drop_on_gray_llm_same(self, mock_llm_judge_true):
        """dry_run=True → gray-zone LLM "same" verdict returns is_dup=False."""
        result = _make_result(score=0.25, mid="existing-id")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, [result], dry_run=True,
        )
        assert is_dup is False
        assert reason.startswith("dry_run_would_llm_drop")
        assert existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_dry_run_emits_structured_decision_log(
        self, mock_llm_judge_false, caplog,
    ):
        """[DEDUP_DECISION decision=hard_drop dry_run=True ...] line emitted."""
        import logging as _logging
        caplog.set_level(_logging.INFO, logger="services.mem0.dedup")
        result = _make_result(score=0.05, mid="existing-id")
        await dedup_mod.check_near_dup(
            "cand text", None, [result], dry_run=True,
        )
        joined = "\n".join(r.message for r in caplog.records)
        assert "DEDUP_DECISION" in joined
        assert "decision=hard_drop" in joined
        assert "dry_run=True" in joined

    @pytest.mark.asyncio
    async def test_non_dry_run_still_drops(self, mock_llm_judge_false):
        """dry_run=False (default) preserves the legacy hard_drop semantics."""
        result = _make_result(score=0.05, mid="existing-id")
        is_dup, reason, existing_id, _conflict, _merged = await dedup_mod.check_near_dup(
            "candidate text", None, [result], dry_run=False,
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")


# ---------------------------------------------------------------------------
# llm_judge_same_fact error-path tests
# ---------------------------------------------------------------------------

class TestLlmJudgeSameFact:
    @pytest.mark.asyncio
    async def test_llm_judge_error_returns_false(self, monkeypatch):
        """get_anthropic_client raises → fail-open returns False."""
        async def _raise():
            raise RuntimeError("network error")

        # Imported lazily inside llm_judge_same_fact; patch at source module.
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", _raise)
        result = await dedup_mod.llm_judge_same_fact("existing text", "candidate text")
        assert result is False

    @pytest.mark.asyncio
    async def test_llm_judge_client_none_returns_false(self, monkeypatch):
        """get_anthropic_client returns None → fail-open returns False."""
        async def _none():
            return None

        # Imported lazily inside llm_judge_same_fact; patch at source module.
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", _none)
        result = await dedup_mod.llm_judge_same_fact("existing text", "candidate text")
        assert result is False


class TestAufhebungMerge:
    """止揚: gray-zone superset/complementary facts synthesize instead of drop."""

    @pytest.mark.asyncio
    async def test_mergeable_synthesizes_and_archives(self, mock_relation_mergeable):
        result = _make_result(score=0.30, text="大学からプログラミングを始めた",
                              mid="old-id")
        is_dup, reason, existing_id, conflict, merged = await dedup_mod.check_near_dup(
            "大学一年から独学でプログラミングを始めた", None, [result],
        )
        assert is_dup is False          # candidate is NOT dropped
        assert conflict == "merge"
        assert existing_id == "old-id"  # old row gets archived
        assert merged == "MERGED: 大学一年から独学でプログラミングを始めた"
        assert reason.startswith("merged")

    @pytest.mark.asyncio
    async def test_mergeable_disabled_falls_back_to_drop(self, mock_relation_mergeable):
        result = _make_result(score=0.30, mid="old-id")
        is_dup, reason, existing_id, conflict, merged = await dedup_mod.check_near_dup(
            "cand", None, [result], allow_merge=False,
        )
        assert is_dup is True           # legacy behavior: drop the superset
        assert conflict is None
        assert merged is None
        assert reason.startswith("merge_disabled_drop")

    @pytest.mark.asyncio
    async def test_mergeable_dry_run_does_not_merge(self, mock_relation_mergeable):
        result = _make_result(score=0.30, mid="old-id")
        is_dup, reason, existing_id, conflict, merged = await dedup_mod.check_near_dup(
            "cand", None, [result], dry_run=True,
        )
        assert is_dup is False
        assert conflict is None
        assert merged is None
        assert reason.startswith("dry_run_would_merge")

    @pytest.mark.asyncio
    async def test_synthesis_failure_keeps_both(self, monkeypatch):
        async def _judge(e, c):
            return "mergeable"
        async def _synth_fail(e, c):
            return None  # synthesis errored → fail-open
        monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _judge)
        monkeypatch.setattr("services.mem0.dedup.synthesize_facts", _synth_fail)
        result = _make_result(score=0.30, mid="old-id")
        is_dup, reason, existing_id, conflict, merged = await dedup_mod.check_near_dup(
            "cand", None, [result],
        )
        assert is_dup is False          # keep candidate
        assert conflict is None         # do NOT archive the existing one
        assert existing_id is None
        assert reason.startswith("merge_failed_keep_both")

    @pytest.mark.asyncio
    async def test_relation_judge_fail_open_is_different(self, monkeypatch):
        async def _raise():
            raise RuntimeError("net")
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", _raise)
        assert await dedup_mod.judge_fact_relation("a", "b") == "different"

    @pytest.mark.asyncio
    async def test_synthesize_fail_open_is_none(self, monkeypatch):
        async def _raise():
            raise RuntimeError("net")
        monkeypatch.setattr("services.mem0.client.get_anthropic_client", _raise)
        assert await dedup_mod.synthesize_facts("a", "b") is None

    @pytest.mark.asyncio
    async def test_llm_judge_same_fact_shim_maps_paraphrase_true(self, monkeypatch):
        async def _p(e, c):
            return "paraphrase"
        monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _p)
        assert await dedup_mod.llm_judge_same_fact("a", "b") is True

    @pytest.mark.asyncio
    async def test_llm_judge_same_fact_shim_maps_different_false(self, monkeypatch):
        async def _d(e, c):
            return "different"
        monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _d)
        assert await dedup_mod.llm_judge_same_fact("a", "b") is False


class TestPartitionMergeable:
    """P3 over-merge gate (ADR-0017)."""

    @pytest.mark.asyncio
    async def test_distinct_aspects_split(self, monkeypatch):
        async def _rel(a, b): return "different"
        monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _rel)
        parts = await dedup_mod.partition_mergeable(["A好き", "A業務で使う", "A非同期に関心"])
        assert len(parts) == 3  # all distinct → no fusion

    @pytest.mark.asyncio
    async def test_same_fact_one_group(self, monkeypatch):
        async def _rel(a, b): return "paraphrase"
        monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _rel)
        parts = await dedup_mod.partition_mergeable(["A好き", "Aが好き", "Aを好む"])
        assert len(parts) == 1
        assert set(parts[0]) == {0, 1, 2}

    @pytest.mark.asyncio
    async def test_exact_dup_skips_llm(self, monkeypatch):
        calls = {"n": 0}
        async def _rel(a, b):
            calls["n"] += 1
            return "different"
        monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _rel)
        # two identical → union via text equality, no judge call between them
        parts = await dedup_mod.partition_mergeable(["同じ", "同じ"])
        assert len(parts) == 1
        assert calls["n"] == 0  # exact-dup pair never hits the LLM

    @pytest.mark.asyncio
    async def test_mixed_paraphrase_and_distinct(self, monkeypatch):
        # 0,1 paraphrase; 2 different from both
        async def _rel(a, b):
            return "paraphrase" if {"好き", "好む"} & {a, b} == {a, b} else (
                "paraphrase" if a in ("好き", "好む") and b in ("好き", "好む") else "different"
            )
        monkeypatch.setattr("services.mem0.dedup.judge_fact_relation", _rel)
        parts = await dedup_mod.partition_mergeable(["好き", "好む", "質問した"])
        sizes = sorted(len(p) for p in parts)
        assert sizes == [1, 2]

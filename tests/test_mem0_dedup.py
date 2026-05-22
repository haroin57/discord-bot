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
def mock_llm_judge_true(monkeypatch):
    async def _judge(existing, candidate):
        return True
    monkeypatch.setattr("services.mem0.dedup.llm_judge_same_fact", _judge)


@pytest.fixture
def mock_llm_judge_false(monkeypatch):
    async def _judge(existing, candidate):
        return False
    monkeypatch.setattr("services.mem0.dedup.llm_judge_same_fact", _judge)


# ---------------------------------------------------------------------------
# check_near_dup tests
# ---------------------------------------------------------------------------

class TestCheckNearDup:
    @pytest.mark.asyncio
    async def test_check_near_dup_hard_drop(self, mock_llm_judge_false):
        """score=0.10 is within hard-drop threshold → (True, hard_drop…, id)."""
        result = _make_result(score=0.10, mid="existing-id")
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")
        assert existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_check_near_dup_novel(self, mock_llm_judge_false):
        """score=0.50 is above gray-zone threshold → (False, 'novel', None)."""
        result = _make_result(score=0.50, mid="existing-id")
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_check_near_dup_gray_zone_same(self, mock_llm_judge_true):
        """score=0.25 (gray zone), LLM says same → (True, llm_judged_same…, id)."""
        result = _make_result(score=0.25, mid="existing-id")
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is True
        assert reason.startswith("llm_judged_same")
        assert existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_check_near_dup_gray_zone_different(self, mock_llm_judge_false):
        """score=0.25 (gray zone), LLM says different → (False, 'novel', None)."""
        result = _make_result(score=0.25, mid="existing-id")
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_check_near_dup_empty_results(self):
        """Empty near_results list → (False, 'novel', None)."""
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", None, []
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_check_near_dup_category_mismatch_skipped(self):
        """Different-category memory is skipped even at hard-drop distance."""
        result = _make_result(score=0.05, mid="existing-id", category="personal")
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", "tech", [result]
        )
        assert is_dup is False
        assert reason == "novel"
        assert existing_id is None

    @pytest.mark.asyncio
    async def test_check_near_dup_same_category_not_skipped(self):
        """Same category does not skip dedup — hard_drop fires normally."""
        result = _make_result(score=0.05, mid="existing-id", category="personal")
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", "personal", [result]
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")
        assert existing_id == "existing-id"

    @pytest.mark.asyncio
    async def test_check_near_dup_no_candidate_category_does_not_skip(self):
        """candidate_category=None means no category filter; hard_drop fires."""
        result = _make_result(score=0.05, mid="existing-id", category="personal")
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", None, [result]
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")

    @pytest.mark.asyncio
    async def test_check_near_dup_no_existing_category_does_not_skip(self):
        """existing_category missing → category filter does not skip."""
        result = _make_result(score=0.05, mid="existing-id")  # no metadata
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
            "candidate text", "tech", [result]
        )
        assert is_dup is True
        assert reason.startswith("hard_drop")

    # ----- Phase A-3: dry_run logging -----

    @pytest.mark.asyncio
    async def test_dry_run_does_not_drop_on_hard_drop(self, mock_llm_judge_false):
        """dry_run=True → hard_drop distance returns is_dup=False with prefix."""
        result = _make_result(score=0.05, mid="existing-id")
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
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
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
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
        is_dup, reason, existing_id, _conflict = await dedup_mod.check_near_dup(
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

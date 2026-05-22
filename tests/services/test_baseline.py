"""Tests for services.emotion.baseline (LLM-derived rest anchor)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.emotion import baseline as baseline_module
from services.emotion.baseline import (
    NUMERIC_FIELDS,
    _extract_json_object,
    _validate,
    baseline_age_hours,
    estimate_baseline_via_llm,
    get_or_estimate_baseline,
    load_baseline,
    save_baseline,
)


_GOOD_BASELINE = {
    "valence": 0.4,
    "arousal": 0.5,
    "loneliness": 0.3,
    "affection": 0.85,
    "curiosity": 0.55,
    "anxiety": 0.2,
}


# ---------------------------------------------------------------------------
# load_baseline
# ---------------------------------------------------------------------------


class TestLoadBaseline:
    def test_missing_file_returns_none(self, tmp_path: Path):
        assert load_baseline(tmp_path) is None

    def test_round_trip(self, tmp_path: Path):
        save_baseline(_GOOD_BASELINE, tmp_path)
        loaded = load_baseline(tmp_path)
        assert loaded is not None
        for axis in NUMERIC_FIELDS:
            assert loaded[axis] == pytest.approx(_GOOD_BASELINE[axis])

    def test_corrupt_json_returns_none(self, tmp_path: Path):
        (tmp_path / "kanae_baseline.json").write_text("not json{}")
        assert load_baseline(tmp_path) is None

    def test_missing_axis_returns_none(self, tmp_path: Path):
        bad = {axis: 0.5 for axis in NUMERIC_FIELDS if axis != "affection"}
        (tmp_path / "kanae_baseline.json").write_text(
            json.dumps({"baseline": bad, "generated_at": "2026-05-17T00:00:00+00:00"})
        )
        assert load_baseline(tmp_path) is None

    def test_non_dict_top_level_returns_none(self, tmp_path: Path):
        (tmp_path / "kanae_baseline.json").write_text(json.dumps(["not", "a", "dict"]))
        assert load_baseline(tmp_path) is None


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_accepts_well_formed(self):
        out = _validate({"baseline": _GOOD_BASELINE, "rationale": "ok"})
        for axis in NUMERIC_FIELDS:
            assert out[axis] == pytest.approx(_GOOD_BASELINE[axis])

    def test_missing_baseline_key_raises(self):
        with pytest.raises(ValueError, match="baseline"):
            _validate({"rationale": "missing field"})

    def test_missing_axis_raises(self):
        bad = dict(_GOOD_BASELINE)
        del bad["affection"]
        with pytest.raises(ValueError, match="affection"):
            _validate({"baseline": bad})

    def test_clamps_to_bounds(self):
        out = _validate({"baseline": {**_GOOD_BASELINE, "affection": 2.5}})
        assert out["affection"] == 1.0  # clamped to upper bound

        out = _validate({"baseline": {**_GOOD_BASELINE, "valence": -3.0}})
        assert out["valence"] == -1.0  # clamped to lower bound

    def test_non_numeric_value_raises(self):
        bad = {**_GOOD_BASELINE, "affection": "high"}
        with pytest.raises(ValueError):
            _validate({"baseline": bad})


# ---------------------------------------------------------------------------
# estimate_baseline_via_llm
# ---------------------------------------------------------------------------


class TestExtractJsonObject:
    def test_pure_json(self):
        out = _extract_json_object('{"baseline": {"valence": 0.5}}')
        assert out == {"baseline": {"valence": 0.5}}

    def test_strips_preamble(self):
        out = _extract_json_object(
            '既に前のターンで出力済みです：\n\n{"baseline": {"valence": 0.5}}'
        )
        assert out == {"baseline": {"valence": 0.5}}

    def test_strips_code_fence(self):
        out = _extract_json_object('```json\n{"baseline": {"valence": 0.5}}\n```')
        assert out == {"baseline": {"valence": 0.5}}

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="no JSON"):
            _extract_json_object("just some plain text")


@pytest.fixture
def _stub_context(monkeypatch):
    """Stub all context collectors so estimate tests don't touch real state."""
    monkeypatch.setattr(
        baseline_module, "_collect_context",
        lambda state_dir: {
            "persona": "stub persona",
            "emotion_journal": "stub journal",
            "daily_recent": "stub daily",
            "daily_summary": "stub summary",
            "mem0_context": "stub mem0",
            "previous_baseline": "stub prev",
            "now": baseline_module.datetime(2026, 5, 17, 12, 0,
                                            tzinfo=baseline_module.JST),
        },
    )


class TestEstimate:
    def test_happy_path(self, tmp_path: Path, monkeypatch, _stub_context):
        monkeypatch.setattr(
            baseline_module, "_call_claude_cli",
            lambda *a, **kw: {"baseline": _GOOD_BASELINE, "rationale": "test"},
        )
        out = estimate_baseline_via_llm(tmp_path)
        assert out["affection"] == pytest.approx(0.85)

    def test_cli_failure_propagates(self, tmp_path: Path, monkeypatch, _stub_context):
        def _boom(*a, **kw):
            raise RuntimeError("simulated CLI failure")

        monkeypatch.setattr(baseline_module, "_call_claude_cli", _boom)
        with pytest.raises(RuntimeError, match="simulated"):
            estimate_baseline_via_llm(tmp_path)

    def test_cli_malformed_raises(self, tmp_path: Path, monkeypatch, _stub_context):
        monkeypatch.setattr(
            baseline_module, "_call_claude_cli",
            lambda *a, **kw: {"oops": "no baseline key"},
        )
        with pytest.raises(ValueError):
            estimate_baseline_via_llm(tmp_path)


class TestPromptIncludesContext:
    """Smoke-test that _build_prompt actually weaves every slice into the text."""

    def test_all_slices_present(self):
        prompt = baseline_module._build_prompt(
            persona="PERSONA_TOKEN_xyz",
            emotion_journal="JOURNAL_TOKEN_abc",
            daily_recent="DAILY_TOKEN_def",
            daily_summary="SUMMARY_TOKEN_ghi",
            mem0_context="MEM0_TOKEN_jkl",
            previous_baseline="PREV_TOKEN_mno",
            now=baseline_module.datetime(2026, 5, 17, 12, 0,
                                          tzinfo=baseline_module.JST),
        )
        for token in (
            "PERSONA_TOKEN_xyz",
            "JOURNAL_TOKEN_abc",
            "DAILY_TOKEN_def",
            "SUMMARY_TOKEN_ghi",
            "MEM0_TOKEN_jkl",
            "PREV_TOKEN_mno",
        ):
            assert token in prompt
        # Anti-echo-chamber language must survive prompt edits
        assert "引きずられず" in prompt or "固着しない" in prompt


# ---------------------------------------------------------------------------
# get_or_estimate_baseline
# ---------------------------------------------------------------------------


class TestGetOrEstimate:
    def test_cache_hit_skips_llm(self, tmp_path: Path, monkeypatch, _stub_context):
        save_baseline(_GOOD_BASELINE, tmp_path)
        called = {"count": 0}

        def _fake_cli(*a, **kw):
            called["count"] += 1
            return {"baseline": _GOOD_BASELINE}

        monkeypatch.setattr(baseline_module, "_call_claude_cli", _fake_cli)
        out = get_or_estimate_baseline(tmp_path)
        assert out["affection"] == pytest.approx(0.85)
        assert called["count"] == 0

    def test_cache_miss_calls_llm_and_persists(self, tmp_path: Path, monkeypatch, _stub_context):
        monkeypatch.setattr(
            baseline_module, "_call_claude_cli",
            lambda *a, **kw: {"baseline": _GOOD_BASELINE},
        )
        assert not (tmp_path / "kanae_baseline.json").exists()
        out = get_or_estimate_baseline(tmp_path)
        assert out["affection"] == pytest.approx(0.85)
        # File should now exist
        assert (tmp_path / "kanae_baseline.json").is_file()
        # And a second call hits cache without LLM
        called = {"count": 0}

        def _fake_cli(*a, **kw):
            called["count"] += 1
            return None

        monkeypatch.setattr(baseline_module, "_call_claude_cli", _fake_cli)
        out2 = get_or_estimate_baseline(tmp_path)
        assert out2["affection"] == pytest.approx(0.85)
        assert called["count"] == 0

    def test_cache_miss_llm_failure_propagates(self, tmp_path: Path, monkeypatch, _stub_context):
        def _boom(*a, **kw):
            raise RuntimeError("CLI down")

        monkeypatch.setattr(baseline_module, "_call_claude_cli", _boom)
        with pytest.raises(RuntimeError, match="CLI down"):
            get_or_estimate_baseline(tmp_path)


# ---------------------------------------------------------------------------
# baseline_age_hours
# ---------------------------------------------------------------------------


class TestAge:
    def test_no_file_returns_none(self, tmp_path: Path):
        assert baseline_age_hours(tmp_path) is None

    def test_fresh_file_returns_small_number(self, tmp_path: Path):
        save_baseline(_GOOD_BASELINE, tmp_path)
        age = baseline_age_hours(tmp_path)
        assert age is not None
        assert age >= 0.0
        assert age < 1.0  # fresh write should be well under an hour

    def test_missing_timestamp_returns_none(self, tmp_path: Path):
        (tmp_path / "kanae_baseline.json").write_text(
            json.dumps({"baseline": _GOOD_BASELINE})
        )
        assert baseline_age_hours(tmp_path) is None

from __future__ import annotations

import json

import pytest

from scripts.analyze_token_efficiency_experiments import analyze_experiment, summarize_usage
from scripts.experiment_memory_queries import CASES
from scripts.experiment_replay_windows import synthetic_update
from scripts.experiment_token_efficiency import synthetic_history
from scripts.token_experiment_quality import assess_memory_representations, assess_replay_answer


@pytest.mark.parametrize(
    ("text", "stop_reason", "failure", "semantic_pass"),
    [
        ('{"code":"SILVER-42","retry_limit":7}', "end_turn", "", True),
        ('```json\n{"code":"SILVER-42","retry_limit":7}\n```', "end_turn", "json_code_fence", True),
        ('{"code":"SILVER-42","retry_limit":7.0}', "end_turn", "unexpected_json_schema", True),
        ('{"code":"WRONG","retry_limit":7}', "end_turn", "unexpected_json_value", False),
        ('{"code":"SILVER-42","retry_limit":7,"extra":true}', "end_turn", "unexpected_json_schema", False),
        ("null", "end_turn", "unexpected_json_schema", False),
        ("", "end_turn", "empty_response", False),
        ("not JSON", "end_turn", "invalid_json", False),
        ("", "refusal", "refusal", False),
        ('{"code":"SILVER-42","retry_limit":7}', "max_tokens", "output_truncated", False),
        ('{"code":"SILVER-42","retry_limit":7}', None, "unexpected_stop_reason", False),
    ],
)
def test_replay_quality_preserves_strict_gate_and_distinguishes_failures(text, stop_reason, failure, semantic_pass):
    result = assess_replay_answer(text, stop_reason)

    assert result["failure_class"] == failure
    assert result["quality_pass"] is (not failure)
    assert result["semantic_quality_pass"] is semantic_pass
    assert result["response_chars"] == len(text)
    assert len(result["response_sha256"]) == 64
    assert "SILVER-42" not in json.dumps(result)


def test_bundle_cannot_pass_by_preserving_terms_while_inventing_prior_discussion():
    generated = [
        "Rust async Python asyncio",
        "RustはDropベースで、PythonはCancelledErrorで伝播するという違いを議論した。",
        "Rust asyncio",
    ]

    result = assess_memory_representations(CASES[3], generated, recall=True)

    assert result["missing_terms"] == []
    assert result["unsupported_hypothesis_terms"] == ["Drop", "CancelledError"]
    assert result["quality_pass"] is False


def test_baseline_hypothesis_without_added_details_still_passes():
    result = assess_memory_representations(
        CASES[3], ["Rust asyncio", "Rust asyncとPython asyncioを比較した議論", "Rust asyncio"],
        recall=True,
    )

    assert result["quality_pass"] is True


@pytest.mark.parametrize("representations", [[], [None, "Rust asyncio"], "Rust asyncio"])
def test_invalid_representation_shapes_fail_closed(representations):
    result = assess_memory_representations(CASES[3], representations, recall=True)

    assert result["representation_shape_valid"] is False
    assert result["quality_pass"] is False


def test_usage_keeps_missing_counters_unknown_instead_of_claiming_zero():
    result = summarize_usage([
        {"usage": {"input_tokens": 10, "cache_read_input_tokens": True, "output_tokens": 4}},
    ])

    assert result["input_tokens"] == 10
    assert result["known_context_tokens"] == 10
    assert result["missing_counters"]["cache_creation_input_tokens"] == 1
    assert result["missing_counters"]["cache_read_input_tokens"] == 1
    assert result["usage_complete"] is False
    assert result["cache_read_fraction"] is None


def test_empty_usage_does_not_prove_complete_measurement():
    result = summarize_usage([])

    assert result["requests"] == 0
    assert result["usage_complete"] is False
    assert result["cache_read_fraction"] is None


def test_memory_quality_requires_completed_provider_calls():
    report = analyze_experiment({"synthetic": True, "results": [
        {"arm": "bundle", "case": 3, "representations": ["Rust asyncio", "Rust asyncio", "Rust asyncio"],
         "lexical_top2_recall": True, "missing_terms": [], "calls": []},
    ]}, kind="memory")

    assert report["arms"]["bundle"]["requests"] == 0
    assert report["arms"]["bundle"]["quality_pass_count"] == 0
    assert report["arms"]["bundle"]["usage_complete"] is False


def test_historical_refusal_is_not_counted_as_an_ordinary_json_failure():
    report = analyze_experiment({"synthetic": True, "results": [
        {"arm": "rolling", "quality_pass": False, "failure_class": "invalid_json", "stop_reason": "refusal", "usage": {}},
    ]}, kind="replay")

    assert report["arms"]["rolling"]["failure_classes"] == {"refusal": 1}
    assert report["arms"]["rolling"]["quality_pass_count"] == 0
    assert report["arms"]["rolling"]["usage_complete"] is False


def test_memory_usage_counts_each_provider_call_without_mixing_model_prices():
    report = analyze_experiment({"synthetic": True, "results": [
        {"arm": "parallel", "case": 3, "representations": ["Rust asyncio", "Rust asyncio", "Rust asyncio"],
         "lexical_top2_recall": True, "missing_terms": [], "calls": [
             {"model": "haiku", "stop_reason": "end_turn", "usage": {"input_tokens": 100}},
             {"model": "sonnet", "stop_reason": "end_turn", "usage": {"input_tokens": 200}},
         ]},
    ]}, kind="memory")

    assert report["arms"]["parallel"]["requests"] == 2
    assert report["arms"]["parallel"]["quality_denominator"] == 1
    assert report["models"]["haiku"]["input_tokens"] == 100
    assert report["models"]["sonnet"]["input_tokens"] == 200


def test_new_replay_updates_keep_calibration_density_without_repeating_answer():
    initial = synthetic_history("same-marker")[:2]
    update = synthetic_update("same-marker", 3)

    assert [row["role"] for row in update] == ["user", "assistant"]
    assert update[0]["content"].count("public synthetic calibration text.") == 24
    assert len(update[0]["content"]) >= len(initial[0]["content"])
    assert "SILVER-42" not in update[0]["content"]
    assert "retry limit" not in update[0]["content"]


def test_non_synthetic_input_is_rejected():
    with pytest.raises(ValueError, match="synthetic"):
        analyze_experiment({"results": []}, kind="replay")

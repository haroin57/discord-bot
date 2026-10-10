from copy import deepcopy

import pytest

from scripts.assess_token_efficiency_performance import assess


def report():
    rows = []
    for case in range(6):
        for arm, count, duration in (("baseline", 6, 1000), ("singleflight", 3, 900)):
            calls = [{"usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                                 "cache_creation_input_tokens": 30, "output_tokens": 4},
                      "duration_ms": duration, "valid_json": True, "stop_reason": "end_turn"}
                     for _ in range(count)]
            rows.append({"case": case, "arm": arm, "control": "concurrent_same_query_two",
                         "consumers": 2, "consumer_quality": [{"quality_pass": True}] * 2,
                         "dispatch_count_pass": True, "provider_calls_complete": True,
                         "shared_outputs_equal": True, "independent_result_lists": True,
                         "duration_ms": duration, "calls": calls})
    return {"synthetic": True, "live_provider_usage": True, "status": "complete",
            "requests_attempted": 54, "request_gap_seconds": 0,
            "implementation_sha256": "same-payload", "results": rows}


def test_empty_production_is_not_zero_percent_savings():
    result = assess([report()] * 3, {"main": {"requests": 0}})
    assert result["paired_groups"] == 18
    assert result["duplicate_context_reduction_percent"] == 50
    assert result["median_latency_change_percent"] == -10
    assert result["production"]["whole_production_reduction_percent"] is None
    assert result["production"]["rate_status"] == "no_post_change_requests"


def test_production_without_comparable_cohort_is_not_a_rate():
    result = assess([report()], {"main": {"requests": 5}})
    assert result["production"]["whole_production_reduction_percent"] is None
    assert result["production"]["rate_status"] == "no_normalized_comparable_cohort"


def test_paced_trials_cannot_prove_latency_improvement():
    payload = report()
    payload["request_gap_seconds"] = 1
    with pytest.raises(ValueError, match="unpaced"):
        assess([payload], {"main": {"requests": 0}})


def test_quality_failure_is_retained_not_filtered():
    payload = report()
    payload["results"][1]["consumer_quality"][0] = {"quality_pass": False}
    assert assess([payload], {"main": {"requests": 0}})["all_quality_dispatch_and_completion_checks_pass"] is False


def test_mismatched_implementations_cannot_be_combined():
    payload = deepcopy(report())
    payload["implementation_sha256"] = "different-payload"
    with pytest.raises(ValueError, match="changed"):
        assess([report(), payload], {"main": {"requests": 0}})


def test_unpaired_cases_cannot_be_compared():
    payload = report()
    payload["results"].pop()
    with pytest.raises(ValueError, match="paired"):
        assess([payload], {"main": {"requests": 0}})

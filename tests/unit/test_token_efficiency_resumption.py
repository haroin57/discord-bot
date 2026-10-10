"""Counter accounting for live experiment checkpoints."""

from copy import deepcopy

import pytest

from scripts.analyze_token_efficiency_resumption import summarize_query, usage
from scripts.audit_token_usage import analyze


def call():
    return {"usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                      "cache_creation_input_tokens": 30, "output_tokens": 4},
            "duration_ms": 100, "stop_reason": "end_turn"}


def test_cached_inputs_are_counted_once():
    result = usage([call()])
    assert result["context_tokens"] == 60
    assert result["context_plus_output_tokens"] == 64


@pytest.mark.parametrize("value", [None, -1, True, "10"])
def test_incomplete_or_invalid_usage_is_not_zero(value):
    record = call()
    record["usage"]["input_tokens"] = value
    with pytest.raises(ValueError):
        usage([record])


def test_checkpoint_duplicates_are_not_double_counted():
    calls = [call(), call(), call()]
    row = {"arm": "singleflight", "control": "concurrent_same_query_two",
           "consumers": 2, "consumer_quality": [{"quality_pass": True}] * 2,
           "dispatch_count_pass": True, "provider_calls_complete": True,
           "shared_outputs_equal": True, "independent_result_lists": True,
           "duration_ms": 200, "calls": calls}
    report = {"synthetic": True, "live_provider_usage": True, "status": "complete",
              "requests_attempted": 3, "results": [row], "completed_provider_calls": deepcopy(calls)}
    result = summarize_query(report)
    assert result["completed_usage"]["completed_requests"] == 3
    assert result["completed_usage"]["context_plus_output_tokens"] == 192
    assert result["concurrent_arms"]["singleflight"]["consumers"] == 2


def test_failed_checkpoint_preserves_unassigned_completions():
    report = {"synthetic": True, "live_provider_usage": True, "status": "failed",
              "requests_attempted": 2, "results": [], "completed_provider_calls": [call()],
              "failed_attempts": [{"usage": "unknown"}]}
    result = summarize_query(report)
    assert result["completed_usage"]["completed_requests"] == 1
    assert result["requests_attempted"] == 2
    assert result["failed_attempts"] == [{"usage": "unknown"}]


def test_simulation_cannot_be_presented_as_live():
    with pytest.raises(ValueError):
        summarize_query({"synthetic": True, "live_provider_usage": False})


def test_helper_task_breakdown_preserves_response_deduplication():
    from json import dumps

    def record(task, request_id):
        return dumps({"msg": f"llm_helper_usage model=sonnet task_id={task} "
                      "input_tokens=10 cache_read_input_tokens=20 cache_creation_input_tokens=30 "
                      f"output_tokens=4 stop_reason=end_turn request_id={request_id}"})

    result = analyze([record("extract", "first"), record("extract", "first"), record("rerank", "second")])
    assert result["measured_helpers_by_model"]["sonnet"]["requests"] == 2
    assert result["measured_helpers_by_task"]["extract"]["sonnet"]["requests"] == 1
    assert result["measured_helpers_by_task"]["rerank"]["sonnet"]["context_tokens"] == 60

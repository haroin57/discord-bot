import logging
from types import SimpleNamespace

from services.token_usage import log_compatible_helper_usage, log_helper_failure, log_helper_usage
from scripts.audit_token_usage import analyze


def test_helper_records_counts_without_prompt_or_response_content(caplog):
    response = SimpleNamespace(usage=dict(input_tokens=5, cache_read_input_tokens=10,
                                         cache_creation_input_tokens=20, output_tokens=3),
                               content=[SimpleNamespace(type="text", text="secret-output")], stop_reason="end_turn")
    with caplog.at_level(logging.INFO):
        log_helper_usage(response, model="sonnet", task_prompt="secret-prompt", user_chars=12, duration_ms=1)
    message = caplog.records[-1].getMessage()
    assert "secret" not in message
    assert "input_tokens=5" in message
    assert analyze([message])["measured_helpers_by_model"]["sonnet"]["context_tokens"] == 35


def test_missing_usage_is_unknown_not_zero(caplog):
    response = SimpleNamespace(content=[], stop_reason=None)
    with caplog.at_level(logging.INFO):
        log_helper_usage(response, model="sonnet", task_prompt="task", user_chars=1, duration_ms=1)
    message = caplog.records[-1].getMessage()
    assert "input_tokens=-" in message
    assert analyze([message])["measured_helpers_by_model"] == {}
    assert analyze([message])["incomplete_helper_usage_observations"] == {"sonnet": 1}


def test_compatible_prompt_and_reasoning_subsets_are_not_added_to_totals(caplog):
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=30, total_tokens=130,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=10)),
        choices=[SimpleNamespace(message=SimpleNamespace(content="secret-output"), finish_reason="length")],
        id="response-one",
    )
    with caplog.at_level(logging.INFO):
        log_compatible_helper_usage(response, model="compatible-model", task_prompt="secret-prompt", user_chars=12, duration_ms=1)
    message = caplog.records[-1].getMessage()
    assert "secret" not in message
    report = analyze([message])
    summary = report["compatible_helpers_by_model"]["compatible-model"]
    assert summary["known_totals"] == {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                                        "cached_prompt_tokens": 80, "reasoning_tokens": 10}
    assert summary["prompt_completion_complete"] is True
    assert summary["finish_reasons"] == {"length": 1}
    assert report["measured_helpers_by_model"] == {}
    assert report["main"]["requests"] == 0


def test_invalid_and_absent_compatible_counters_remain_unknown(caplog):
    response = {"usage": {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": -1},
                "choices": [], "id": "response-missing"}
    with caplog.at_level(logging.INFO):
        log_compatible_helper_usage(response, model="compatible-model", task_prompt="task", user_chars=1, duration_ms=1)
    message = caplog.records[-1].getMessage()
    assert "prompt_tokens=-" in message
    assert "cached_prompt_tokens=-" in message
    summary = analyze([message])["compatible_helpers_by_model"]["compatible-model"]
    assert summary["missing_counter_responses"]["prompt_tokens"] == 1
    assert summary["missing_counter_responses"]["cached_prompt_tokens"] == 1
    assert summary["measured_counter_responses"]["completion_tokens"] == 1
    assert summary["prompt_completion_complete"] is False


def test_helper_response_ids_deduplicate_repeated_telemetry(caplog):
    response = SimpleNamespace(usage=dict(input_tokens=5, cache_read_input_tokens=10,
                                         cache_creation_input_tokens=20, output_tokens=3),
                               content=[], stop_reason="end_turn", id="same-response")
    with caplog.at_level(logging.INFO):
        log_helper_usage(response, model="sonnet", task_prompt="task", user_chars=1, duration_ms=1)
    message = caplog.records[-1].getMessage()
    assert analyze([message, message])["measured_helpers_by_model"]["sonnet"]["requests"] == 1
    without_id = message.replace("request_id=same-response", "request_id=-")
    assert analyze([without_id, without_id])["measured_helpers_by_model"]["sonnet"]["requests"] == 2


def test_compatible_response_ids_deduplicate_without_merging_models(caplog):
    response = {"usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
                "choices": [], "id": "same-response"}
    with caplog.at_level(logging.INFO):
        log_compatible_helper_usage(response, model="compatible-model", task_prompt="task", user_chars=1, duration_ms=1)
    message = caplog.records[-1].getMessage()
    report = analyze([message, message, message.replace("model=compatible-model", "model=other-model")])
    assert report["compatible_helpers_by_model"]["compatible-model"]["responses"] == 1
    assert report["compatible_helpers_by_model"]["other-model"]["responses"] == 1


def test_failure_diagnostics_never_log_error_payload_or_infer_zero_consumption(caplog):
    with caplog.at_level(logging.INFO):
        log_helper_failure(route="anthropic_cli", model="sonnet", task_prompt="secret-task",
                           user_chars=12, duration_ms=1, outcome="failed", error=TimeoutError("secret-token"))
    message = caplog.records[-1].getMessage()
    assert "secret" not in message
    assert "error_type=TimeoutError" in message
    assert "usage=unknown" in message
    report = analyze([message])
    assert report["helper_failure_observations"] == {"anthropic_cli": {"failed": 1}}
    assert report["measured_helpers_by_model"] == {}


def test_known_partial_helper_counters_are_retained_without_claiming_complete_context(caplog):
    response = SimpleNamespace(usage={"input_tokens": 5, "output_tokens": 7}, content=[], stop_reason="max_tokens")
    with caplog.at_level(logging.INFO):
        log_helper_usage(response, model="sonnet", task_prompt="task", user_chars=1, duration_ms=1)
    report = analyze([caplog.records[-1].getMessage()])
    partial = report["incomplete_helper_usage_by_model"]["sonnet"]
    assert partial["known_totals"]["input_tokens"] == 5
    assert partial["known_totals"]["output_tokens"] == 7
    assert partial["missing_counter_responses"]["cache_read_input_tokens"] == 1
    assert partial["missing_counter_responses"]["cache_creation_input_tokens"] == 1
    assert partial["stop_reasons"] == {"max_tokens": 1}
    assert report["measured_helpers_by_model"] == {}

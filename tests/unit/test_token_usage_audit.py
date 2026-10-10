import json

from scripts.audit_token_usage import analyze, decode_record


def usage(request_id="req-test", **overrides):
    values = dict(channel=123, turn=1, model="claude-opus-5-5", input_tokens=2,
                  cache_read_input_tokens=100, cache_creation_input_tokens=50,
                  output_tokens=10, stop_reason="end_turn", request_id=request_id,
                  message_count=255)
    values.update(overrides)
    return "claude_api_usage " + " ".join(f"{key}={value}" for key, value in values.items())


def test_deduplicates_requests_and_ignores_loop_totals():
    result = analyze([usage(), usage(), usage().replace("claude_api_usage ", "claude_api_usage_total ")])
    assert result["main"]["requests"] == 1
    assert result["main"]["context_tokens"] == 152
    assert result["main"]["output_tokens"] == 10


def test_journal_timestamp_and_nested_application_record():
    message = json.dumps(dict(msg=usage(), ts="2026-10-10T00:00:00", rid="private"))
    line = json.dumps(dict(MESSAGE=message, __REALTIME_TIMESTAMP="1000000"))
    decoded = decode_record(line)
    assert decoded[1] == "1970-01-01T00:00:01+00:00"
    result = analyze([line])
    assert result["correlated_request_counts"] == {1: 1}
    assert "private" not in json.dumps(result)
    assert "123" not in json.dumps(result)


def test_missing_usage_and_invalid_records_are_not_zero_measurements():
    result = analyze(["claude_api_usage model=foo", "[]", "garbage", usage(input_tokens="-")])
    assert result["main"]["requests"] == 0


def test_models_cohorts_and_stop_reasons():
    result = analyze([usage(), usage("req-other", model="sonnet", message_count=3, stop_reason="tool_use")])
    assert result["models"]["sonnet"]["requests"] == 1
    assert result["history_cohorts"]["history_under_250"]["requests"] == 1
    assert result["main"]["stop_reasons"] == {"end_turn": 1, "tool_use": 1}


def test_auxiliary_and_compaction_counts_are_separate():
    result = analyze(["[MEM0_METRICS query_len=44 candidates=20 kept=0 rerank=on]",
                      "delib_planner trivial=True", "delib_judge_lite ok=False",
                      "compaction summary response: input_tokens=100 output_tokens=20"])
    assert result["memory_search_observations"] == {"searches": 1, "rerank_on": 1, "zero_kept": 1}
    assert result["compaction_observations"]["input_tokens"] == 100
    assert result["main"]["input_tokens"] == 0


def test_requests_without_ids_are_not_merged():
    assert analyze([usage("-"), usage("-")])["main"]["requests"] == 2


def test_shared_consumers_are_not_counted_as_paid_provider_requests():
    result = analyze([
        "mem0_query_singleflight outcome=leader active=1 waiters=1",
        "mem0_query_singleflight outcome=joined active=1 waiters=2",
        "mem0_query_singleflight outcome=bypass reason=inflight_limit",
        "mem0_query_singleflight outcome=invalid",
        usage().replace("claude_api_usage ", "llm_helper_usage "),
    ])

    assert result["memory_query_singleflight_observations"] == {"leader": 1, "joined": 1, "bypass": 1}
    assert result["memory_query_singleflight_bypass_reasons"] == {"inflight_limit": 1}
    assert result["measured_helpers_by_model"]["claude-opus-5-5"]["requests"] == 1
    assert result["main"]["requests"] == 0


def test_nearby_cache_pattern_requires_same_channel_and_short_gap():
    records = [json.dumps(dict(msg=usage("first", cache_read_input_tokens=32307,
                                       cache_creation_input_tokens=140000), ts="2026-10-10T00:00:00")),
               json.dumps(dict(msg=usage("next", cache_read_input_tokens=32307,
                                       cache_creation_input_tokens=140000), ts="2026-10-10T00:05:00")),
               json.dumps(dict(msg=usage("late", cache_read_input_tokens=32307,
                                       cache_creation_input_tokens=140000), ts="2026-10-10T02:00:00"))]
    assert analyze(records)["nearby_small_cache_read_observations"] == {"requests": 1, "cache_creation_input_tokens": 140000}


def test_payload_diagnostics_do_not_retain_identifiers():
    result = analyze(["[PROMPT_BREAKDOWN ch=123] api_payload: prior_chars=42 tools=84 system_chars=13372"])
    assert result["payload_observations"] == {"records": 1, "tools_min": 84, "tools_max": 84, "system_chars_max": 13372}


def test_missing_latency_is_not_zero():
    result = analyze([usage(duration_ms="-", ttft_ms="-"), usage("other", duration_ms=-1, ttft_ms="oops")])
    assert result["main_per_request_latency"]["duration_ms"] == {
        "measured_requests": 0, "missing_requests": 2, "median": None, "p95": None, "max": None,
    }


def test_latency_is_deduplicated_with_usage():
    result = analyze([usage(duration_ms=1000, ttft_ms=200), usage(duration_ms=1000, ttft_ms=200),
                      usage("other", duration_ms=2000, ttft_ms=400)])
    assert result["main_per_request_latency"]["duration_ms"] == {
        "measured_requests": 2, "missing_requests": 0, "median": 1500, "p95": 1950, "max": 2000,
    }
    assert result["main_per_request_latency"]["ttft_ms"]["median"] == 300


def test_latency_models_remain_separate_and_helpers_are_excluded():
    result = analyze([usage(duration_ms=500, ttft_ms=100),
                      usage("other", model="sonnet", duration_ms=1500, ttft_ms=200),
                      usage("helper", duration_ms=9000).replace("claude_api_usage ", "llm_helper_usage ")])
    assert result["main_per_request_latency"]["duration_ms"]["measured_requests"] == 2
    assert result["model_per_request_latency"]["sonnet"]["duration_ms"]["median"] == 1500


def test_empty_latency_has_no_denominator():
    assert analyze([])["main_per_request_latency"]["duration_ms"]["median"] is None


def test_operational_warnings_are_separate_from_usage():
    lines = [json.dumps({"logger": "services.claude_api.stall", "level": "WARNING",
                        "msg": "[ch=123] stream stall: no message for 35s"}),
             json.dumps({"logger": "bot_record", "level": "WARNING",
                         "msg": "Mem0 batch retained for retry: channel=123 reason=private"})]
    result = analyze(lines)
    assert result["operational_observations"] == {"stream_stall_warnings": 1, "memory_batches_retained_for_retry": 1}
    assert result["main"]["requests"] == 0
    assert "private" not in json.dumps(result)


def test_journal_operational_warnings_preserve_attribution():
    record = {"logger": "services.claude_api.stall", "level": "WARNING",
              "msg": "[ch=123] stream idle for 600s, cancelling"}
    result = analyze([json.dumps({"MESSAGE": json.dumps(record)})])
    assert result["operational_observations"] == {"stream_idle_cancellations": 1}


def test_unattributed_or_wrong_level_markers_are_not_operational_warnings():
    result = analyze(["Mem0 batch retained for retry: channel=123",
                      json.dumps({"logger": "unrelated", "level": "WARNING", "msg": "stream stall: no message"}),
                      json.dumps({"logger": "bot_record", "level": "INFO", "msg": "Mem0 retry persistence failed: private"}),
                      json.dumps({"MESSAGE": "plain log line"})])
    assert result["operational_observations"] == {}


def test_retry_persistence_failure_is_not_success():
    record = {"logger": "bot_record", "level": "WARNING", "msg": "Mem0 retry persistence failed: OSError"}
    result = analyze([json.dumps(record)])
    assert result["operational_observations"] == {"memory_retry_persistence_failures": 1}

"""Aggregate token counters from journal JSON or application JSONL on stdin."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import re
from statistics import median
import sys


FIELDS = (
    "input_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "output_tokens",
)
COMPATIBLE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens", "reasoning_tokens")
PAIR = re.compile(r"([a-z_]+)=([^\s]+)")


def decode_record(line: str) -> tuple[str, str, str]:
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return line.strip(), "", ""
    if not isinstance(record, dict):
        return "", "", ""
    journal_timestamp = record.get("__REALTIME_TIMESTAMP")
    timestamp = ""
    if journal_timestamp:
        try:
            timestamp = datetime.fromtimestamp(
                int(journal_timestamp) / 1_000_000, timezone.utc,
            ).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
    if "MESSAGE" in record:
        message, application_timestamp, correlation = decode_record(str(record["MESSAGE"]))
        return message, timestamp or application_timestamp, correlation
    return str(record.get("msg", "")), timestamp or str(record.get("ts", "")), str(record.get("rid", ""))


def log_metadata(line: str) -> tuple[str, str]:
    try:
        record = json.loads(line)
        if isinstance(record, dict) and "MESSAGE" in record:
            record = json.loads(record["MESSAGE"])
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(record, dict):
        return "", ""
    return str(record.get("logger", "")), str(record.get("level", ""))


def summarize(rows: list[dict]) -> dict:
    totals = {field: sum(row[field] for row in rows) for field in FIELDS}
    contexts = sorted(sum(row[field] for field in FIELDS[:3]) for row in rows)
    context_total = sum(contexts)
    return {
        "requests": len(rows), **totals,
        "context_tokens": context_total,
        "cache_read_fraction": round(totals["cache_read_input_tokens"] / context_total, 4) if context_total else 0,
        "context_median": median(contexts) if contexts else 0,
        "context_max": max(contexts, default=0),
        "stop_reasons": dict(Counter(row.get("stop_reason", "unknown") for row in rows)),
    }


def summarize_compatible(rows: list[dict]) -> dict:
    measured = {field: sum(row.get(field) is not None for row in rows) for field in COMPATIBLE_FIELDS}
    return {
        "responses": len(rows),
        "known_totals": {field: sum(row[field] for row in rows if row.get(field) is not None) for field in COMPATIBLE_FIELDS},
        "measured_counter_responses": measured,
        "missing_counter_responses": {field: len(rows) - measured[field] for field in COMPATIBLE_FIELDS},
        "prompt_completion_complete": bool(rows) and all(row.get("prompt_tokens") is not None and row.get("completion_tokens") is not None for row in rows),
        "finish_reasons": dict(Counter(row.get("finish_reason", "unknown") for row in rows)),
    }


def summarize_incomplete_helpers(rows: list[dict]) -> dict:
    return {
        "responses": len(rows),
        "known_totals": {field: sum(row[field] for row in rows if row.get(field) is not None) for field in FIELDS},
        "missing_counter_responses": {field: sum(row.get(field) is None for row in rows) for field in FIELDS},
        "stop_reasons": dict(Counter(row.get("stop_reason", "unknown") for row in rows)),
    }


def summarize_latency(rows: list[dict]) -> dict:
    result = {}
    for field in ("duration_ms", "ttft_ms"):
        values = sorted(row[field] for row in rows if row.get(field) is not None)
        if values:
            position = (len(values) - 1) * 0.95
            lower = int(position)
            upper = min(lower + 1, len(values) - 1)
            p95 = values[lower] + (values[upper] - values[lower]) * (position - lower)
        else:
            p95 = None
        result[field] = {
            "measured_requests": len(values), "missing_requests": len(rows) - len(values),
            "median": median(values) if values else None,
            "p95": p95, "max": max(values) if values else None,
        }
    return result


def analyze(lines) -> dict:
    rows = []
    seen = set()
    models = defaultdict(list)
    cohorts = defaultdict(list)
    memory = Counter()
    query_singleflight = Counter()
    query_singleflight_bypass = Counter()
    auxiliary = Counter()
    timestamps = []
    compaction = Counter()
    correlations = Counter()
    payloads = []
    previous_channel_times = {}
    nearby_prefix_only = Counter()
    helpers = defaultdict(list)
    helper_tasks = defaultdict(lambda: defaultdict(list))
    compatible_helpers = defaultdict(list)
    incomplete_helpers = Counter()
    incomplete_helper_rows = defaultdict(list)
    helper_failures = defaultdict(Counter)
    seen_helper_ids = set()
    operational = Counter()
    for line in lines:
        message, timestamp, correlation = decode_record(line)
        logger, level = log_metadata(line)
        if logger == "services.claude_api.stall" and level == "WARNING":
            if "stream stall: no message" in message:
                operational["stream_stall_warnings"] += 1
            if "stream idle for" in message:
                operational["stream_idle_cancellations"] += 1
        if logger == "bot_record" and level == "WARNING":
            if message.startswith("Mem0 batch retained for retry:"):
                operational["memory_batches_retained_for_retry"] += 1
            if message.startswith("Mem0 retry persistence failed:"):
                operational["memory_retry_persistence_failures"] += 1
        if message.startswith(("llm_helper_usage ", "llm_compatible_helper_usage ")):
            values = dict(PAIR.findall(message))
            request_id = values.get("request_id", "-")
            if request_id != "-":
                helper_key = (message.split(" ", 1)[0], values.get("model", "unknown"), request_id)
                if helper_key in seen_helper_ids:
                    continue
                seen_helper_ids.add(helper_key)
        if message.startswith("llm_compatible_helper_usage "):
            values = dict(PAIR.findall(message))
            compatible_helpers[values.get("model", "unknown")].append({
                **{field: int(values[field]) if values.get(field, "").isdigit() else None for field in COMPATIBLE_FIELDS},
                "finish_reason": values.get("finish_reason", "unknown"),
            })
        if message.startswith("llm_helper_failure "):
            values = dict(PAIR.findall(message))
            helper_failures[values.get("route", "unknown")][values.get("outcome", "unknown")] += 1
        if "mem0_query_singleflight " in message:
            values = dict(PAIR.findall(message))
            outcome = values.get("outcome")
            if outcome in {"leader", "joined", "bypass"}:
                query_singleflight[outcome] += 1
                if outcome == "bypass":
                    query_singleflight_bypass[values.get("reason", "unknown")] += 1
        if message.startswith("llm_helper_usage "):
            values = dict(PAIR.findall(message))
            if all(values.get(field, "").isdigit() for field in FIELDS):
                model = values.get("model", "unknown")
                helper_row = {
                    **{field: int(values[field]) for field in FIELDS},
                    "stop_reason": values.get("stop_reason", "unknown"),
                }
                helpers[model].append(helper_row)
                helper_tasks[values.get("task_id", "unknown")][model].append(helper_row)
            else:
                model = values.get("model", "unknown")
                incomplete_helpers[model] += 1
                incomplete_helper_rows[model].append({
                    **{field: int(values[field]) if values.get(field, "").isdigit() else None for field in FIELDS},
                    "stop_reason": values.get("stop_reason", "unknown"),
                })
        if "api_payload:" in message and "PROMPT_BREAKDOWN" in message:
            values = dict(PAIR.findall(message))
            payloads.append({key: int(values[key]) for key in ("system_chars", "tools", "prior_chars") if values.get(key, "").isdigit()})
        if "[MEM0_METRICS " in message:
            values = dict(PAIR.findall(message))
            memory["searches"] += 1
            memory["rerank_on"] += values.get("rerank", "").rstrip("]") == "on"
            memory["zero_kept"] += values.get("kept") == "0"
        for stage in ("delib_planner", "delib_judge_lite"):
            if stage in message:
                auxiliary[stage + "_log_records"] += 1
        if "compaction summary response:" in message:
            values = dict(PAIR.findall(message))
            compaction["responses"] += 1
            for field in ("input_tokens", "output_tokens"):
                if values.get(field, "").isdigit():
                    compaction[field] += int(values[field])
        if not message.startswith("claude_api_usage "):
            continue
        values = dict(PAIR.findall(message))
        if not all(values.get(field, "").isdigit() for field in FIELDS):
            continue
        request_id = values.get("request_id", "-")
        if request_id != "-":
            if request_id in seen:
                continue
            seen.add(request_id)
        row = {field: int(values[field]) for field in FIELDS}
        row["stop_reason"] = values.get("stop_reason", "unknown")
        for field in ("duration_ms", "ttft_ms"):
            row[field] = int(values[field]) if values.get(field, "").isdigit() else None
        if timestamp:
            try:
                current = datetime.fromisoformat(timestamp)
                channel = values.get("channel", "")
                previous = previous_channel_times.get(channel)
                previous_channel_times[channel] = current
                gap = (current - previous).total_seconds() if previous else None
                if (channel and gap is not None and 0 <= gap < 3600
                    and values.get("turn") == "1"
                    and 0 < row["cache_read_input_tokens"] <= 35000
                    and row["cache_creation_input_tokens"] > 100000):
                    nearby_prefix_only["requests"] += 1
                    nearby_prefix_only["cache_creation_input_tokens"] += row["cache_creation_input_tokens"]
            except (ValueError, TypeError):
                pass
        rows.append(row)
        models[values.get("model", "unknown")].append(row)
        if correlation:
            correlations[correlation] += 1
        count = int(values.get("message_count", "0")) if values.get("message_count", "0").isdigit() else 0
        cohorts["history_250_plus" if count >= 250 else "history_under_250"].append(row)
        if timestamp:
            timestamps.append(timestamp)
    return {
        "first_record": min(timestamps, default=""),
        "last_record": max(timestamps, default=""),
        "main": summarize(rows),
        "models": {model: summarize(group) for model, group in sorted(models.items())},
        "main_per_request_latency": summarize_latency(rows),
        "model_per_request_latency": {model: summarize_latency(group) for model, group in sorted(models.items())},
        "history_cohorts": {name: summarize(group) for name, group in sorted(cohorts.items())},
        "correlated_request_counts": dict(sorted(Counter(correlations.values()).items())),
        "nearby_small_cache_read_observations": dict(nearby_prefix_only),
        "memory_search_observations": dict(memory),
        "memory_query_singleflight_observations": dict(query_singleflight),
        "memory_query_singleflight_bypass_reasons": dict(query_singleflight_bypass),
        "auxiliary_observations": dict(auxiliary),
        "measured_helpers_by_model": {model: summarize(group) for model, group in sorted(helpers.items())},
        "measured_helpers_by_task": {
            task: {model: summarize(group) for model, group in sorted(groups.items())}
            for task, groups in sorted(helper_tasks.items())
        },
        "compatible_helpers_by_model": {model: summarize_compatible(group) for model, group in sorted(compatible_helpers.items())},
        "incomplete_helper_usage_observations": dict(incomplete_helpers),
        "incomplete_helper_usage_by_model": {model: summarize_incomplete_helpers(group) for model, group in sorted(incomplete_helper_rows.items())},
        "helper_failure_observations": {route: dict(outcomes) for route, outcomes in sorted(helper_failures.items())},
        "operational_observations": dict(operational),
        "compaction_observations": dict(compaction),
        "payload_observations": {
            "records": len(payloads),
            "tools_min": min((row["tools"] for row in payloads if "tools" in row), default=0),
            "tools_max": max((row["tools"] for row in payloads if "tools" in row), default=0),
            "system_chars_max": max((row["system_chars"] for row in payloads if "system_chars" in row), default=0),
        },
        "limitations": [
            "Main per-request counters only; loop totals are excluded to avoid double counting.",
            "Latency covers completed main API requests, not end-to-end Discord responses or unlogged interrupted requests.",
            "Missing latency is unknown, not zero; sample percentiles are descriptive, not proof of equivalent workloads or regression.",
            "Operational warnings are attributed diagnostic records, not failed API requests, proven regressions, or measured token consumption.",
            "The main logger can include verifier/subagent loops; correlation groups are not necessarily single conversations.",
            "Nearby small cache reads are a diagnostic pattern, not proof of a specific cache invalidation cause.",
            "Missing request IDs cannot be deduplicated; unlogged or interrupted requests are not counted.",
            "Auxiliary log records and memory searches are not token measurements or exact API call counts.",
            "Single-flight observations describe dispatch decisions; saved tokens are not inferred from joined consumers.",
            "Compatible-helper prompt totals already include cached tokens; optional cache/reasoning subsets are not added again.",
            "Helper failures, cancellations, unavailable clients, and incomplete usage are observations, not zero-consumption measurements.",
            "Helper response IDs deduplicate repeated records when present; SDK retries and unreported partial consumption remain unknown.",
            "Task and model breakdowns describe the same helper responses; never add both breakdowns together.",
            "Compaction logs omit cache counters; these partial totals are separate from main usage.",
            "Cached input is context volume, not equivalent billing cost; no price estimate is made.",
            "Application-only timestamps lack a timezone; journal input supplies authoritative UTC timestamps.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    json.dump(analyze(sys.stdin), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

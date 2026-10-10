"""Summarize resumed synthetic trials without double-counting checkpoint data."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import median


FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def usage(rows):
    totals = dict.fromkeys(FIELDS, 0)
    for row in rows:
        counters = row["usage"]
        for field in FIELDS:
            value = counters.get(field)
            if type(value) is not int or value < 0:
                raise ValueError("A completed response has missing or invalid usage")
            totals[field] += value
    totals["context_tokens"] = sum(totals[field] for field in FIELDS[:3])
    totals["context_plus_output_tokens"] = totals["context_tokens"] + totals["output_tokens"]
    return {"completed_requests": len(rows), **totals}


def summarize_query(report):
    if report.get("synthetic") is not True or report.get("live_provider_usage") is not True:
        raise ValueError("A live, explicitly synthetic query trial is required")
    # completed_provider_calls duplicates the calls nested under results on success.
    groups = report.get("results", [])
    measured = report.get("completed_provider_calls", [call for row in groups for call in row["calls"]])
    arms = {}
    for arm in ("baseline", "singleflight"):
        rows = [row for row in groups if row["arm"] == arm and row["control"] == "concurrent_same_query_two"]
        if not rows:
            continue
        calls = [call for row in rows for call in row["calls"]]
        arms[arm] = {
            **usage(calls), "cases": len(rows),
            "consumers": sum(row["consumers"] for row in rows),
            "consumer_quality_passes": sum(item["quality_pass"] for row in rows for item in row["consumer_quality"]),
            "all_dispatch_counts_pass": all(row["dispatch_count_pass"] for row in rows),
            "all_provider_calls_complete": all(row["provider_calls_complete"] for row in rows),
            "all_shared_outputs_equal": all(row["shared_outputs_equal"] for row in rows) if arm == "singleflight" else None,
            "all_independent_result_lists": all(row["independent_result_lists"] for row in rows),
            "group_duration_median_ms": median(row["duration_ms"] for row in rows),
            "provider_duration_median_ms": median(call["duration_ms"] for call in calls),
            "stop_reasons": dict(Counter(call["stop_reason"] for call in calls)),
        }
    sequential = [row for row in groups if row["control"] == "sequential_same_query_two"]
    return {
        "status": report["status"], "requests_attempted": report["requests_attempted"],
        "completed_usage": usage(measured), "failed_attempts": report.get("failed_attempts", []),
        "request_gap_seconds": report.get("request_gap_seconds"),
        "max_provider_concurrency": report.get("max_provider_concurrency"),
        "concurrent_arms": arms,
        "sequential_control": [
            {"requests_attempted": row["requests_attempted"], "dispatch_count_pass": row["dispatch_count_pass"],
             "consumer_quality_passes": sum(item["quality_pass"] for item in row["consumer_quality"]),
             **usage(row["calls"])} for row in sequential
        ],
    }


def summarize_replay(report):
    if report.get("synthetic") is not True:
        raise ValueError("An explicitly synthetic replay trial is required")
    arms = {}
    for arm in ("rolling", "anchored"):
        rows = [row for row in report["results"] if row["arm"] == arm]
        if rows:
            arms[arm] = {
                **usage(rows), "strict_quality_passes": sum(row["quality_pass"] for row in rows),
                "semantic_quality_passes": sum(row["semantic_quality_pass"] for row in rows),
                "failures": dict(Counter(row["failure_class"] for row in rows if row["failure_class"])),
                "head_changes": sum(row["head_changed"] for row in rows),
            }
    return {"status": report["status"], "requests_attempted": report["requests_attempted"],
            "completed_usage": usage(report["results"]), "failed_attempts": report.get("failed_attempts", []),
            "arms": arms}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", type=Path, required=True)
    parser.add_argument("--unpaced-query", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    args = parser.parse_args()
    reports, hashes = {}, {}
    for name, path in (("query", args.query), ("unpaced_query", args.unpaced_query), ("replay", args.replay)):
        data = path.read_bytes()
        hashes[str(path)] = hashlib.sha256(data).hexdigest()
        reports[name] = json.loads(data)
    summary = {
        "source_sha256": hashes, "query": summarize_query(reports["query"]),
        "unpaced_query": summarize_query(reports["unpaced_query"]),
        "replay": summarize_replay(reports["replay"]),
        "scope": [
            "Public synthetic trials; no production-wide reduction rate or billing estimate.",
            "Each completed provider response is counted once, despite duplicate checkpoint lists.",
            "Paced group durations are not production latency; the unpaced pilot has one case only.",
            "Failed attempts have unknown consumption, not zero consumption.",
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Assess unpaced, repeated live trials separately from natural production traffic."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from statistics import median
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.analyze_token_efficiency_resumption import summarize_query


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def assess(reports, production):
    if not reports:
        raise ValueError("No live performance trials were supplied")
    arms = {arm: {"requests": 0, "context_tokens": 0, "output_tokens": 0,
                  "quality_passes": 0, "consumers": 0, "durations": []}
            for arm in ("baseline", "singleflight")}
    pairs = []
    all_quality_pass = True
    implementation_hashes = set()
    for report in reports:
        if (report.get("status") != "complete" or report.get("request_gap_seconds") != 0
                or report.get("failed_attempts")):
            raise ValueError("Performance trials must be complete, failure-free, and unpaced")
        summary = summarize_query(report)
        implementation_hashes.add(report["implementation_sha256"])
        rows = [row for row in report["results"] if row["control"] == "concurrent_same_query_two"]
        for row in report["results"]:
            all_quality_pass &= all(item["quality_pass"] for item in row["consumer_quality"])
            all_quality_pass &= all(call["valid_json"] and call["stop_reason"] == "end_turn" for call in row["calls"])
            all_quality_pass &= row["dispatch_count_pass"] and row["provider_calls_complete"]
            if row["control"] == "concurrent_same_query_two" and row["arm"] == "singleflight":
                all_quality_pass &= row["shared_outputs_equal"] and row["independent_result_lists"]
        for arm in arms:
            measured = summary["concurrent_arms"][arm]
            arms[arm]["requests"] += measured["completed_requests"]
            arms[arm]["context_tokens"] += measured["context_tokens"]
            arms[arm]["output_tokens"] += measured["output_tokens"]
            arms[arm]["quality_passes"] += measured["consumer_quality_passes"]
            arms[arm]["consumers"] += measured["consumers"]
            arms[arm]["durations"].extend(row["duration_ms"] for row in rows if row["arm"] == arm)
        by_case = {}
        for row in rows:
            by_case.setdefault(row["case"], {})[row["arm"]] = row["duration_ms"]
        if len(rows) != 12 or len(by_case) != 6 or any(set(item) != set(arms) for item in by_case.values()):
            raise ValueError("Each trial must include six paired cases")
        pairs.extend(by_case.values())
    if len(implementation_hashes) != 1:
        raise ValueError("The measured implementation changed between trials")
    for arm in arms.values():
        durations = arm.pop("durations")
        arm["latency_median_ms"] = median(durations)
        arm["latency_p95_ms"] = percentile(durations, 0.95)
        arm["context_plus_output_tokens"] = arm["context_tokens"] + arm["output_tokens"]
    before, after = arms["baseline"], arms["singleflight"]
    input_reduction = 1 - after["context_tokens"] / before["context_tokens"]
    combined_reduction = 1 - after["context_plus_output_tokens"] / before["context_plus_output_tokens"]
    latency_change = after["latency_median_ms"] / before["latency_median_ms"] - 1
    natural_helpers = sum(item["requests"] for item in production.get("measured_helpers_by_model", {}).values())
    natural_main = production["main"]["requests"]
    # No normalized, comparable production before/after cohort was supplied.
    # Zero requests are an empty denominator, never a measured 0% reduction.
    return {
        "implementation_sha256": next(iter(implementation_hashes)),
        "repetitions": len(reports), "paired_groups": len(pairs),
        "live_requests_attempted": sum(report["requests_attempted"] for report in reports),
        "all_quality_dispatch_and_completion_checks_pass": bool(all_quality_pass),
        "concurrent_duplicate_arms": arms,
        "duplicate_context_reduction_percent": round(input_reduction * 100, 4),
        "duplicate_context_plus_output_reduction_percent": round(combined_reduction * 100, 4),
        "median_latency_change_percent": round(latency_change * 100, 4),
        "median_paired_latency_ratio": median(item["singleflight"] / item["baseline"] for item in pairs),
        "production": {"main_requests_observed": natural_main, "helper_requests_observed": natural_helpers,
                       "dispatch_decisions": production.get("memory_query_singleflight_observations", {}),
                       "whole_production_reduction_percent": None,
                       "rate_status": "no_post_change_requests" if natural_main + natural_helpers == 0 else "no_normalized_comparable_cohort"},
        "scope": [
            "Latency is public synthetic query expansion with real HTTP requests, not Discord end-to-end latency.",
            "Six query cases repeated three times are 18 pairs, not 18 independent query populations.",
            "Synthetic duplicate reduction is not a production-wide reduction or a billing reduction.",
            "A favorable sample median does not establish equivalence or exclude future tail-latency regressions.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-report", type=Path, action="append", required=True)
    parser.add_argument("--production-after", type=Path, required=True)
    args = parser.parse_args()
    reports = [json.loads(path.read_text()) for path in args.live_report]
    result = assess(reports, json.loads(args.production_after.read_text()))
    result["source_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in [*args.live_report, args.production_after]}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

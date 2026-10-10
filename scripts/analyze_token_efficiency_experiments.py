"""Recompute provider counters and quality gates from saved synthetic experiments."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.experiment_memory_queries import CASES
from scripts.token_experiment_quality import assess_memory_representations


FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def summarize_usage(calls: list[dict]) -> dict:
    counters = dict.fromkeys(FIELDS, 0)
    missing = dict.fromkeys(FIELDS, 0)
    for call in calls:
        usage = call.get("usage") or {}
        for field in FIELDS:
            value = usage.get(field)
            if type(value) is int and value >= 0:
                counters[field] += value
            else:
                missing[field] += 1
    context = sum(counters[field] for field in FIELDS[:3])
    complete = bool(calls) and not any(missing.values())
    return {
        "requests": len(calls),
        **counters,
        "known_context_tokens": context,
        "usage_complete": complete,
        "missing_counters": missing,
        "cache_read_fraction": counters["cache_read_input_tokens"] / context if complete and context else None,
    }


def analyze_experiment(payload: dict, *, kind: str) -> dict:
    if payload.get("synthetic") is not True:
        raise ValueError("Only explicitly synthetic experiments are accepted")
    if kind not in {"memory", "replay"}:
        raise ValueError("Unknown experiment kind")
    grouped = defaultdict(list)
    models = defaultdict(list)
    for row in payload.get("results", []):
        grouped[row["arm"]].append(row)
        for call in row.get("calls", [row]):
            models[call.get("model") or payload.get("model") or "unknown"].append(call)
    summaries = {}
    cases = payload.get("cases", CASES)
    for arm, rows in sorted(grouped.items()):
        calls = [call for row in rows for call in row.get("calls", [row])]
        passed = []
        quality_issues = []
        failures = Counter()
        for row in rows:
            if kind == "memory":
                quality = assess_memory_representations(
                    cases[row["case"]], row.get("representations", []),
                    recall=row.get("lexical_top2_recall") is True,
                )
                if quality["unsupported_hypothesis_terms"]:
                    quality_issues.append({"case": row["case"], **quality})
                stops_ok = bool(row.get("calls")) and all(call.get("stop_reason") == "end_turn" for call in row.get("calls", []))
                is_pass = quality["quality_pass"] and stops_ok
            else:
                is_pass = row.get("quality_pass") is True
                failure = row.get("failure_class") or ("" if is_pass else "unclassified_historical_failure")
                if row.get("stop_reason") == "refusal":
                    failure = "refusal"
                if failure:
                    failures[failure] += 1
            if is_pass:
                passed.append(row)
        durations = [row["duration_ms"] for row in rows if type(row.get("duration_ms")) is int]
        successful_durations = [row["duration_ms"] for row in passed if type(row.get("duration_ms")) is int]
        summaries[arm] = {
            **summarize_usage(calls),
            "observations": len(rows),
            "quality_pass_count": len(passed),
            "quality_denominator": len(rows),
            "duration_median_ms": statistics.median(durations) if durations else None,
            "successful_duration_median_ms": statistics.median(successful_durations) if successful_durations else None,
            "refusals": sum(call.get("stop_reason") == "refusal" for call in calls),
            "head_changes": sum(row.get("head_changed") is True for row in rows),
            "failure_classes": dict(failures),
            "quality_issues": quality_issues,
        }
        if kind == "memory":
            summaries[arm]["legacy_lexical_pass_count"] = sum(
                not row.get("missing_terms") and row.get("lexical_top2_recall") is True for row in rows
            )
    return {
        "started_utc": payload.get("started_utc"),
        "synthetic": True,
        "arms": summaries,
        "models": {model: summarize_usage(calls) for model, calls in sorted(models.items())},
        "quality_scope": (
            "Post-hoc required-term, lexical-recall, and targeted unsupported-hypothesis checks; not production semantic recall accuracy."
            if kind == "memory" else
            "Original strict JSON results and recorded stop reasons; missing historical response text cannot be reassessed."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, default=ROOT / "docs/2026-10-10-replay-window-experiment.json")
    parser.add_argument("--memory", type=Path, default=ROOT / "docs/2026-10-10-memory-query-experiment.json")
    arguments = parser.parse_args()
    report = {}
    for kind, path in (("replay", arguments.replay), ("memory", arguments.memory)):
        raw = path.read_bytes()
        report[kind] = {
            "source": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            **analyze_experiment(json.loads(raw), kind=kind),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

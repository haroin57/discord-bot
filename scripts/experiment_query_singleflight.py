"""Replay saved public helper responses to control concurrent dispatch counts.

This sends no provider requests. Counter replay is a weighting of saved usage,
not measured current usage, billed savings, or production duplicate frequency.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.analyze_token_efficiency_experiments import FIELDS
from scripts.benchmark_mem0_expansion import load_search
from scripts.experiment_memory_queries import CASES, lexical_score
from scripts.token_experiment_quality import assess_memory_representations


CONTROLS = {
    "single": (1, False, False, False),
    "concurrent_same_query_two": (2, False, False, False),
    "concurrent_same_query_four": (4, False, False, False),
    "concurrent_distinct_queries": (4, True, False, False),
    "concurrent_distinct_owners": (4, False, True, False),
    "sequential_same_query": (4, False, False, True),
}


async def run(source: Path, *, implementation: Path | None = None) -> dict:
    raw = source.read_bytes()
    payload = json.loads(raw)
    if payload.get("synthetic") is not True:
        raise ValueError("Only explicitly synthetic source data is accepted")
    cases = payload.get("cases", CASES)
    parallel_rows = [row for row in payload["results"] if row["arm"] == "parallel"]
    references = {row["case"]: row for row in parallel_rows}
    if not cases or len(parallel_rows) != len(cases) or set(references) != set(range(len(cases))):
        raise ValueError("Source must contain one parallel reference per case")
    by_query = {case["query"]: index for index, case in enumerate(cases)}
    if len(by_query) != len(cases):
        raise ValueError("Source queries must be distinct")
    for row in references.values():
        if (len(row.get("calls", [])) != 3
                or {call.get("stage") for call in row["calls"]} != {
                    "search.rewrite_query", "search.hyde", "search.keyword_extract",
                }
                or any(call.get("stop_reason") != "end_turn" for call in row["calls"])
                or any(type(call["usage"].get(field)) is not int or call["usage"][field] < 0
                       for call in row["calls"] for field in FIELDS)):
            raise ValueError("Source requires three completed, fully measured helper calls")
    source_path = implementation or ROOT / "deploy/performance/mem0_alpha/search.py"
    module = load_search("_singleflight_control", source_path)
    module.settings = SimpleNamespace(MEM0_REWRITE_MODEL="reference-rewrite",
        MEM0_HYDE_MODEL="reference-hyde", MEM0_KEYWORD_MODEL="reference-keyword",
        MEM0_REWRITE_MAX_TOKENS=500, MEM0_HYDE_MAX_TOKENS=200,
        MEM0_KEYWORD_MAX_TOKENS=300, MEM0_HYDE_ENABLED=True)
    previous = {name: os.environ.get(name) for name in (
        "MEM0_QUERY_SINGLEFLIGHT_ENABLED", "MEM0_QUERY_EXPANSION_MODE",
    )}
    groups = defaultdict(lambda: {
        "cases": [], "consumers_checked": 0, "simulated_provider_dispatches": 0,
        "matched_saved_representations": 0, "reference_quality_pass_count": 0,
        "counter_replay_not_live_usage": dict.fromkeys(FIELDS, 0),
    })
    dispatches = []

    async def stage(stage_name, query):
        index = by_query[query]
        row = references[index]
        call = next(call for call in row["calls"] if call["stage"] == stage_name)
        dispatches.append(call)
        await asyncio.sleep(0.005)
        representations = row["representations"]
        if stage_name == "search.rewrite_query":
            return copy.deepcopy(representations[:-2])
        if stage_name == "search.hyde":
            return representations[-2]
        return representations[-1]

    module._rewrite_query = lambda query: stage("search.rewrite_query", query)
    module._generate_hypothetical_fact = lambda query: stage("search.hyde", query)
    module._extract_keywords_via_llm = lambda query: stage("search.keyword_extract", query)
    os.environ["MEM0_QUERY_EXPANSION_MODE"] = "parallel"
    try:
        for index in range(len(cases)):
            for control, (consumers, different_queries, different_owners, sequential) in CONTROLS.items():
                jobs = [((index + offset) % len(cases) if different_queries else index,
                         f"synthetic-owner-{offset}" if different_owners else "synthetic-owner")
                        for offset in range(consumers)]
                for arm in (["baseline", "singleflight"] if index % 2 == 0 else ["singleflight", "baseline"]):
                    os.environ["MEM0_QUERY_SINGLEFLIGHT_ENABLED"] = str(int(arm == "singleflight"))
                    dispatches.clear()

                    async def request(case_index, owner):
                        return await module._shared_query_representations(cases[case_index]["query"], owner)

                    if sequential:
                        results = [await request(case_index, owner) for case_index, owner in jobs]
                    else:
                        results = await asyncio.gather(*(request(case_index, owner) for case_index, owner in jobs))
                    expected = 3 * (len(set(jobs)) if arm == "singleflight" and not sequential else consumers)
                    if len(dispatches) != expected:
                        raise AssertionError(f"Unexpected dispatch count in {control}/{arm}")
                    group = groups[(control, arm)]
                    group["cases"].append(index)
                    group["consumers_checked"] += consumers
                    group["simulated_provider_dispatches"] += len(dispatches)
                    for call in dispatches:
                        for field in FIELDS:
                            group["counter_replay_not_live_usage"][field] += call["usage"][field]
                    for (case_index, owner), result in zip(jobs, results):
                        representations = result[0] + [result[1], result[2]]
                        matched = representations == references[case_index]["representations"]
                        group["matched_saved_representations"] += matched
                        ranks = sorted(range(len(cases)), key=lambda fact: max(
                            (lexical_score(cases[fact]["fact"], query) for query in representations if query), default=0,
                        ), reverse=True)
                        quality = assess_memory_representations(cases[case_index], representations,
                                                                recall=case_index in ranks[:2])
                        group["reference_quality_pass_count"] += matched and quality["quality_pass"]
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return {
        "synthetic": True,
        "simulation": "saved_response_controlled_replay",
        "new_provider_requests": 0,
        "source": str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "implementation_source": str(source_path.relative_to(ROOT)) if source_path.is_relative_to(ROOT) else str(source_path),
        "implementation_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "singleflight_sha256": hashlib.sha256((ROOT / "services/query_expansion.py").read_bytes()).hexdigest(),
        "experiment_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "case_manifest_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "independent_saved_provider_query_samples": len(references),
        "arm_order": "alternating_by_case",
        "quality_scope": "Exact saved-response preservation under controlled replay; not independent new model quality samples or live vector/graph recall.",
        "counter_scope": "Replayed saved provider counters only; not current consumption, billed savings, or production duplicate frequency.",
        "controls": {control: {arm: groups[(control, arm)] for arm in ("baseline", "singleflight")}
                     for control in CONTROLS},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "docs/2026-10-10-memory-query-experiment.json")
    parser.add_argument("--implementation", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(run(arguments.source, implementation=arguments.implementation)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

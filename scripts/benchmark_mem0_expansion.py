"""Compare Mem0 expansion scheduling offline with fixed simulated stage delays.

Supply --baseline pointing at the pre-change llm_mem0/search.py. This loads
both full source files with inert provider imports and verifies identical
queries and stage call counts. Timings are synthetic, not production latency.
"""

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
from types import ModuleType, SimpleNamespace


def load_search(name, path):
    package = ModuleType(name)
    package.__path__ = []
    sys.modules[name] = package
    for suffix, attributes in {
        "client": {"_get_mem0": None, "mem_get_all": None, "mem_search": None},
        "llm": {"complete_json": None, "escape_braces": None},
        "settings": {"settings": SimpleNamespace()},
    }.items():
        dependency = ModuleType(f"{name}.{suffix}")
        dependency.__dict__.update(attributes)
        sys.modules[dependency.__name__] = dependency
    spec = importlib.util.spec_from_file_location(f"{name}.search", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


async def measure(module, delay, repeats):
    calls = []

    async def stage(name, result):
        calls.append(name)
        await asyncio.sleep(delay)
        return result

    async def graph(query, user_id):
        assert user_id == "synthetic-owner"
        return await stage("graph", [query, "alias"])

    def morpho(query):
        calls.append("morpho")
        time.sleep(delay)
        return "morphological"

    module._expand_query_via_graph = graph
    module._expand_query_via_morpho = morpho
    module._rewrite_query = lambda query: stage("rewrite", ["rewritten", "alias"])
    module._generate_hypothetical_fact = lambda query: stage("hyde", "hypothetical")
    module._extract_keywords_via_llm = lambda query: stage("keywords", "rewritten")
    durations = []
    for _ in range(repeats):
        calls.clear()
        start = time.perf_counter()
        result = await module._expand_queries("original", "synthetic-owner", rewrite=True)
        durations.append((time.perf_counter() - start) * 1000)
        assert result == ["original", "alias", "morphological", "rewritten", "hypothetical"]
        assert sorted(calls) == ["graph", "hyde", "keywords", "morpho", "rewrite"]
    return {"median_ms": round(statistics.median(durations), 3), "stage_calls": len(calls)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--stage-delay-ms", type=float, default=50)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if args.stage_delay_ms < 0 or args.repeats < 1:
        parser.error("stage delay must be nonnegative and repeats positive")
    current = Path(__file__).resolve().parents[1] / "deploy/performance/mem0_alpha/search.py"
    report = {"simulated_stage_delay_ms": args.stage_delay_ms, "repeats": args.repeats}
    for name, path in (("before", args.baseline), ("after", current)):
        report[name] = await measure(load_search(f"_expansion_benchmark_{name}", path),
                                     args.stage_delay_ms / 1000, args.repeats)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

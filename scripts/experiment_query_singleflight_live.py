"""Bounded live comparison of owner-scoped concurrent query expansion.

Only public synthetic queries are sent. No memory database, Discord message,
conversation history, or production state is read or changed.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_mem0_expansion import load_search
from scripts.experiment_memory_queries import CASES, lexical_score
from scripts.token_experiment_quality import assess_memory_representations

FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


async def run(args) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    from services.claude_api.client_factory import make_api_client
    from services.mem0.cli_compat import cli_messages_create, extract_text

    module = load_search("_live_singleflight_experiment", args.implementation)
    module.settings = SimpleNamespace(
        MEM0_REWRITE_MODEL=os.environ.get("MEM0_REWRITE_MODEL", "claude-sonnet-4-6"),
        MEM0_HYDE_MODEL=os.environ.get("MEM0_HYDE_MODEL", "claude-haiku-4-5-20251001"),
        MEM0_KEYWORD_MODEL=os.environ.get("MEM0_KEYWORD_MODEL", "claude-haiku-4-5-20251001"),
        MEM0_REWRITE_MAX_TOKENS=500, MEM0_HYDE_MAX_TOKENS=200,
        MEM0_KEYWORD_MAX_TOKENS=300, MEM0_HYDE_ENABLED=True,
    )
    module.escape_braces = lambda text, limit=None: text[:limit].replace("{", "{{").replace("}", "}}")
    client = (await make_api_client(False)).with_options(max_retries=0, timeout=90)
    previous = {name: os.environ.get(name) for name in (
        "MEM0_QUERY_SINGLEFLIGHT_ENABLED", "MEM0_QUERY_EXPANSION_MODE",
    )}
    attempted = 0
    calls = []
    results = []
    ceiling = args.cases * 9 + 6
    started_utc = datetime.now(timezone.utc).isoformat()
    request_lock = asyncio.Lock()
    provider_semaphore = asyncio.Semaphore(args.max_provider_concurrency)
    last_request = 0.0
    failures = []
    report = {"started_utc": started_utc, "synthetic": True, "live_provider_usage": True,
              "request_ceiling": ceiling, "requests_attempted": 0, "max_retries": 0,
              "status": "running", "results": results, "failed_attempts": failures}

    def checkpoint():
        report["requests_attempted"] = attempted
        if args.output:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    async def complete_json(*, system, user_message, model, max_tokens, caller):
        nonlocal attempted, last_request
        started = time.monotonic()
        async with provider_semaphore:
            async with request_lock:
                if failures:
                    raise RuntimeError("experiment stopped after provider failure")
                if attempted >= ceiling:
                    raise RuntimeError("experiment request ceiling reached")
                await asyncio.sleep(max(0, args.request_gap - (time.monotonic() - last_request)))
                attempted += 1
                last_request = time.monotonic()
            provider_started = time.monotonic()
            try:
                response = await cli_messages_create(
                    client, model=model, max_tokens=max_tokens,
                    user_message=user_message, task_prompt=system,
                )
            except BaseException as error:
                failures.append({"stage": caller, "model": model, "error_type": type(error).__name__,
                                 "status_code": getattr(error, "status_code", None), "usage": "unknown"})
                checkpoint()
                raise
        usage = {field: getattr(response.usage, field, None) for field in FIELDS}
        if any(type(value) is not int or value < 0 for value in usage.values()):
            failures.append({"stage": caller, "model": model, "error_type": "IncompleteUsage", "usage": "unknown"})
            checkpoint()
            raise RuntimeError("provider usage is incomplete")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", extract_text(response).strip())
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        calls.append({
            "stage": caller, "model": model, "stop_reason": response.stop_reason,
            "duration_ms": int((time.monotonic() - provider_started) * 1000),
            "queue_wait_ms": int((provider_started - started) * 1000),
            "valid_json": parsed is not None, "usage": usage,
        })
        checkpoint()
        return parsed

    module.complete_json = complete_json
    os.environ["MEM0_QUERY_EXPANSION_MODE"] = "parallel"

    def quality(index, result):
        representations = result[0] + [result[1], result[2]]
        ranks = sorted(range(len(CASES)), key=lambda fact: max(
            (lexical_score(CASES[fact]["fact"], query) for query in representations if query),
            default=0,
        ), reverse=True)
        return assess_memory_representations(CASES[index], representations, recall=index in ranks[:2])

    try:
        for index, case in enumerate(CASES[:args.cases]):
            order = ("baseline", "singleflight") if index % 2 == 0 else ("singleflight", "baseline")
            for arm in order:
                os.environ["MEM0_QUERY_SINGLEFLIGHT_ENABLED"] = str(int(arm == "singleflight"))
                offset = len(calls)
                attempt_offset = attempted
                started = time.monotonic()
                outputs = await asyncio.gather(*(
                    module._shared_query_representations(case["query"], "public-synthetic-owner")
                    for _ in range(2)
                ))
                dispatched = attempted - attempt_offset
                expected = 3 if arm == "singleflight" else 6
                record = {
                    "case": index, "arm": arm, "control": "concurrent_same_query_two",
                    "consumers": 2, "requests_attempted": dispatched,
                    "expected_requests": expected, "dispatch_count_pass": dispatched == expected,
                    "provider_calls_complete": len(calls[offset:]) == expected,
                    "shared_outputs_equal": outputs[0] == outputs[1],
                    "independent_result_lists": outputs[0][0] is not outputs[1][0],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "consumer_quality": [quality(index, output) for output in outputs],
                    "calls": calls[offset:],
                }
                results.append(record)
                checkpoint()
                print(json.dumps(record), file=sys.stderr, flush=True)
        os.environ["MEM0_QUERY_SINGLEFLIGHT_ENABLED"] = "1"
        offset = len(calls)
        attempt_offset = attempted
        outputs = [await module._shared_query_representations(CASES[0]["query"], "public-synthetic-owner")
                   for _ in range(2)]
        results.append({
            "case": 0, "arm": "singleflight", "control": "sequential_same_query_two",
            "consumers": 2, "requests_attempted": attempted - attempt_offset,
            "expected_requests": 6, "dispatch_count_pass": attempted - attempt_offset == 6,
            "provider_calls_complete": len(calls[offset:]) == 6,
            "consumer_quality": [quality(0, output) for output in outputs], "calls": calls[offset:],
        })
        report["status"] = "complete" if not failures else "failed"
    except BaseException as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        report["completed_provider_calls"] = calls
        checkpoint()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        await client.close()
    report.update({
        "started_utc": started_utc, "synthetic": True, "live_provider_usage": True,
        "request_ceiling": ceiling, "requests_attempted": attempted, "max_retries": 0,
        "request_gap_seconds": args.request_gap,
        "max_provider_concurrency": args.max_provider_concurrency,
        "latency_scope": "Group duration includes experimental pacing; provider duration excludes queueing. No production database or graph latency is measured.",
        "arm_order": "alternating_by_case", "cases": CASES[:args.cases],
        "implementation_sha256": hashlib.sha256(args.implementation.read_bytes()).hexdigest(),
        "singleflight_sha256": hashlib.sha256((ROOT / "services/query_expansion.py").read_bytes()).hexdigest(),
        "quality_scope": "Required terms, public synthetic lexical retrieval, targeted unsupported hypotheses, and unchanged shared responses; not production semantic recall accuracy.",
        "production_state_modified": False, "results": results,
    })
    checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--implementation", type=Path, required=True)
    parser.add_argument("--cases", type=int, choices=range(1, 7), default=6)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--request-gap", type=float, default=5.0)
    parser.add_argument("--max-provider-concurrency", type=int, choices=range(1, 7), default=6)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for billable synthetic requests")
    if args.request_gap < 0:
        parser.error("--request-gap must be nonnegative")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

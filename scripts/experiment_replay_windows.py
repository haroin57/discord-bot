"""Compare effective rolling and anchored replay loaders on synthetic API traffic."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.experiment_token_efficiency import synthetic_history
from scripts.token_experiment_quality import assess_replay_answer


def synthetic_update(marker: str, step: int) -> list[dict]:
    pair = synthetic_history(f"{marker}-step-{step}", pairs=2)[:2]
    for row in pair:
        row["timestamp"] = f"new-{step}-{row['role']}"
    return pair


def load_session(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.MAX_MESSAGES_PER_CHANNEL = 12
    return module


async def run(args) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    from services.claude_api.client import MODEL_BETAS
    from services.claude_api.client_factory import make_api_client
    from services.claude_api.request_shape import _build_cached_system, _build_stream_kwargs_impl, _inject_cache_breakpoints

    modules = {"rolling": load_session("_rolling_replay_experiment", args.baseline),
               "anchored": load_session("_anchored_replay_experiment", args.payload)}
    client = (await make_api_client(False)).with_options(max_retries=0, timeout=120)
    results = []
    report = {"synthetic": True, "model": "claude-opus-5-5", "quality_version": 2,
              "arm_order": "alternating_by_round", "update_density": "same_calibration_template",
              "minimum_rows": 24, "extra_rows": 6, "max_requests": 20,
              "requests_attempted": 0, "failed_attempts": [], "status": "running", "results": results}

    def checkpoint():
        if args.output:
            args.output.write_text(json.dumps(report, indent=2) + "\n")

    previous_extra = os.environ.get("CLAUDE_API_REPLAY_EXTRA_ROWS")
    started_utc = datetime.now(timezone.utc).isoformat()
    report["started_utc"] = started_utc
    states = {}
    marker = f"window-{uuid.uuid4()}"
    for position, (arm, module) in enumerate(modules.items()):
        arm_marker = f"{marker}-arm-{position}"
        records = synthetic_history(arm_marker)
        for index, row in enumerate(records):
            row["timestamp"] = f"synthetic-{index}"
        states[arm] = {"records": records, "marker": arm_marker, "previous_head": None}

        def tail(path, maximum, *, source=records, **kwargs):
            return source[-maximum:]

        module._tail_jsonl = tail
    try:
        for step in range(9):
            order = list(modules) if step % 2 == 0 else list(reversed(modules))
            for arm in order:
                module = modules[arm]
                state = states[arm]
                records = state["records"]
                os.environ["CLAUDE_API_REPLAY_EXTRA_ROWS"] = "6" if arm == "anchored" else "0"
                if step:
                    records.extend(synthetic_update(state["marker"], step))
                history, _, _ = module.load_api_replay_channel(100000000000789)
                head = history[0]["content"]
                messages = history + [{"role": "user", "content": "Return only JSON with code and retry_limit for the approved project."}]
                options = _build_stream_kwargs_impl(current_model="claude-opus-5-5",
                    cached_system=_build_cached_system("Synthetic test. Answer exactly as JSON.", messages),
                    messages=_inject_cache_breakpoints(messages), extra_betas=MODEL_BETAS,
                    output_config_override={"effort": "low"}, refusal_fallback_model=None,
                    tools_for_api=[], extra_headers=None, fallback_used=False, server_side_fallback_enabled=False)
                options["max_tokens"] = 1024
                started = time.monotonic()
                for retry in range(3):
                    if report["requests_attempted"] >= report["max_requests"]:
                        raise RuntimeError("experiment request ceiling reached")
                    if report["requests_attempted"]:
                        await asyncio.sleep(args.request_gap)
                    report["requests_attempted"] += 1
                    try:
                        async with client.beta.messages.stream(**options) as stream:
                            response = await stream.get_final_message()
                        break
                    except Exception as error:
                        status = getattr(error, "status_code", None)
                        failure = {"arm": arm, "step": step, "error_type": type(error).__name__,
                                   "status_code": status, "usage": "unknown"}
                        retry_after = getattr(getattr(error, "response", None), "headers", {}).get("retry-after")
                        if retry_after is not None:
                            failure["retry_after_seconds"] = retry_after
                        report["failed_attempts"].append(failure)
                        checkpoint()
                        print(json.dumps(failure), file=sys.stderr, flush=True)
                        if status != 429 or retry == 2 or len(report["failed_attempts"]) > 2:
                            raise
                        try:
                            wait = max(10, float(retry_after or "60"))
                        except (ValueError, TypeError):
                            wait = 60
                        if wait > 120:
                            raise
                        await asyncio.sleep(wait)
                text = "".join(block.text for block in response.content if block.type == "text")
                row = {"arm": arm, "step": step, "history_messages": len(history),
                       "head_changed": state["previous_head"] is not None and head != state["previous_head"],
                       **assess_replay_answer(text, response.stop_reason), "stop_reason": response.stop_reason,
                       "duration_ms": int((time.monotonic() - started) * 1000),
                       "usage": {field: getattr(response.usage, field, 0) or 0 for field in (
                           "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}}
                results.append(row)
                checkpoint()
                state["previous_head"] = head
                print(json.dumps(row), file=sys.stderr, flush=True)
        report["status"] = "complete"
    except BaseException as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        checkpoint()
        if previous_extra is None:
            os.environ.pop("CLAUDE_API_REPLAY_EXTRA_ROWS", None)
        else:
            os.environ["CLAUDE_API_REPLAY_EXTRA_ROWS"] = previous_extra
        await client.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Checkpoint every completed response and failed attempt.")
    parser.add_argument("--request-gap", type=float, default=5.0)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for billable synthetic requests")
    if args.request_gap < 0:
        parser.error("--request-gap must be nonnegative")
    print(json.dumps(asyncio.run(run(args)), indent=2))


if __name__ == "__main__":
    main()

"""Bounded live synthetic cache experiments; no production conversation or state."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.token_experiment_quality import assess_replay_answer


def synthetic_history(marker: str, pairs: int = 12) -> list[dict]:
    history = []
    for index in range(pairs):
        detail = " ".join(f"record-{index}-{item}: public synthetic calibration text." for item in range(24))
        history.extend([
            {"role": "user", "content": f"{marker} calibration record {index}. {detail}"},
            {"role": "assistant", "content": f"Recorded public calibration record {index}."},
        ])
    history[-2]["content"] += " The approved project code is SILVER-42 and the retry limit is 7."
    return history


def experiment_messages(history: list[dict], arm: str, step: int) -> list[dict]:
    messages = list(history)
    if step:
        extra = [{"role": "user", "content": "A new calibration record has arrived."},
                 {"role": "assistant", "content": "Recorded the additional calibration record."}]
        messages = messages[2:] + extra if arm == "rolling" else messages + extra
    messages.append({"role": "user", "content": "Return only JSON with code and retry_limit for the approved project. Do not use tools."})
    return messages


async def run(args) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    from services.claude_api.client import MODEL_BETAS
    from services.claude_api.client_factory import make_api_client
    from services.claude_api.request_shape import _build_cached_system, _build_stream_kwargs_impl, _inject_cache_breakpoints

    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "model": args.model,
              "quality_version": 2,
              "synthetic": True, "max_requests": 10, "results": []}
    client = (await make_api_client(False)).with_options(max_retries=0, timeout=120)
    tools = [{"name": f"calibration_{index}", "description": "Synthetic calibration tool. Do not call.",
              "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}}}
             for index in range(12)]
    try:
        for arm in ("rolling", "append", "tool_order", "canonical_order", "effort_change"):
            history = synthetic_history(f"experiment-{uuid.uuid4()}-{arm}")
            for step in range(2):
                messages = experiment_messages(history, arm, step)
                options = _build_stream_kwargs_impl(
                    current_model=args.model,
                    cached_system=_build_cached_system("Synthetic calibration. Answer the final question exactly as JSON.", messages),
                    messages=_inject_cache_breakpoints(messages), extra_betas=MODEL_BETAS,
                    output_config_override={"effort": "medium" if arm == "effort_change" and step else "low"},
                    refusal_fallback_model=None, tools_for_api=list(reversed(tools)) if arm in {"tool_order", "canonical_order"} and step else tools,
                    extra_headers=None, fallback_used=False, server_side_fallback_enabled=False,
                )
                options["max_tokens"] = 1024
                if arm == "tool_order":
                    options["tools"] = list(reversed(tools)) if step else tools
                started = time.monotonic()
                async with client.beta.messages.stream(**options) as stream:
                    response = await stream.get_final_message()
                text = "".join(block.text for block in response.content if block.type == "text")
                result = {"arm": arm, "step": step, "duration_ms": int((time.monotonic() - started) * 1000),
                          "stop_reason": response.stop_reason,
                          **assess_replay_answer(text, response.stop_reason),
                          "usage": {field: getattr(response.usage, field, 0) or 0 for field in (
                              "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}}
                report["results"].append(result)
                print(json.dumps(result), file=sys.stderr, flush=True)
    finally:
        await client.close()
    report["all_quality_pass"] = all(row["quality_pass"] for row in report["results"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable ten billable synthetic requests.")
    parser.add_argument("--model", default="claude-opus-5-5")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this experiment consumes provider tokens")
    print(json.dumps(asyncio.run(run(args)), indent=2))


if __name__ == "__main__":
    main()

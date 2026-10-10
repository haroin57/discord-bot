"""Bounded synthetic recall-expansion comparison with provider token counters."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
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
from scripts.token_experiment_quality import assess_memory_representations


CASES = [
    {"query": "前に話した青葉プロジェクトは再試行7回のままで、9回にしない条件を覚えている。",
     "required": ["青葉", "7"], "fact": "青葉プロジェクトは再試行7回、9回への変更は禁止です。"},
    {"query": "森川あかりと上原みなとの好きな映画について以前の記憶を検索したい。",
     "required": ["森川あかり", "上原みなと"], "fact": "森川あかりと上原みなとが好きな映画を記録しています。"},
    {"query": "NVX-24を100mgから50mgにしたという架空設定で、夜には使わない条件の記憶を探す。",
     "required": ["NVX-24", "50"], "fact": "架空のNVX-24設定は100mgから50mgへ変更し、夜には使いません。"},
    {"query": "Rust asyncのキャンセル伝播とPython asyncioの差を比較した前の議論を思い出したい。",
     "required": ["Rust", "asyncio"], "fact": "Rust asyncとPython asyncioのキャンセル伝播を比較しました。",
     "forbidden_hypothesis": ["Drop", "CancelledError"]},
    {"query": "Silver Finchをシルバーフィンチとも呼ぶ創作作品のタイトルについて記憶を探したい。",
     "required": ["Silver Finch"], "fact": "創作作品Silver Finchはシルバーフィンチとも呼ばれています。"},
    {"query": "ブルーコーヒーと甘くない紅茶、朝食はトーストと卵という好みを前に話した。",
     "required": ["ブルーコーヒー", "紅茶"], "fact": "ブルーコーヒーと甘くない紅茶が好みで、朝食はトーストと卵です。"},
]


def lexical_score(text: str, query: str) -> float:
    normalize = lambda value: re.sub(r"\s+", "", value.lower())
    text, query = normalize(text), normalize(query)
    query_grams = {query[index:index + 2] for index in range(len(query) - 1)}
    text_grams = {text[index:index + 2] for index in range(len(text) - 1)}
    return len(query_grams & text_grams) / max(1, len(query_grams))


async def run(args) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    from services.claude_api.client_factory import make_api_client
    from services.mem0.cli_compat import cli_messages_create, extract_text

    module = load_search("_memory_query_experiment", ROOT / "deploy/performance/mem0_alpha/search.py")
    module.settings = SimpleNamespace(
        MEM0_REWRITE_MODEL=os.environ.get("MEM0_REWRITE_MODEL", "claude-sonnet-4-6"),
        MEM0_HYDE_MODEL=os.environ.get("MEM0_HYDE_MODEL", "claude-haiku-4-5-20251001"),
        MEM0_KEYWORD_MODEL=os.environ.get("MEM0_KEYWORD_MODEL", "claude-haiku-4-5-20251001"),
        MEM0_REWRITE_MAX_TOKENS=500, MEM0_HYDE_MAX_TOKENS=200,
        MEM0_KEYWORD_MAX_TOKENS=300, MEM0_HYDE_ENABLED=True,
    )
    module.escape_braces = lambda text, limit=None: text[:limit].replace("{", "{{").replace("}", "}}")
    client = (await make_api_client(False)).with_options(max_retries=0, timeout=90)
    records = []
    calls = []
    requests_attempted = 0

    async def complete_json(*, system, user_message, model, max_tokens, caller):
        nonlocal requests_attempted
        if requests_attempted >= 36:
            raise RuntimeError("experiment request ceiling reached")
        requests_attempted += 1
        response = await cli_messages_create(client, model=model, max_tokens=max_tokens,
                                             user_message=user_message, task_prompt=system)
        calls.append({"stage": caller, "model": model, "stop_reason": response.stop_reason,
                      "usage": {field: getattr(response.usage, field, 0) or 0 for field in (
                          "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}})
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", extract_text(response).strip())
        try:
            return json.loads(text)
        except ValueError:
            return None

    module.complete_json = complete_json
    previous_mode = os.environ.get("MEM0_QUERY_EXPANSION_MODE")
    started_utc = datetime.now(timezone.utc).isoformat()
    try:
        for index, case in enumerate(CASES):
            for arm in (["parallel", "bundle"] if index % 2 == 0 else ["bundle", "parallel"]):
                os.environ["MEM0_QUERY_EXPANSION_MODE"] = arm
                start_call = len(calls)
                started = time.monotonic()
                variants, hypothetical, keywords = await module._query_representations(case["query"])
                representations = variants + [hypothetical, keywords]
                corpus = [item["fact"] for item in CASES]
                ranks = sorted(range(len(corpus)), key=lambda fact: max((lexical_score(corpus[fact], query) for query in representations if query), default=0), reverse=True)
                record = {"case": index, "arm": arm, "duration_ms": int((time.monotonic() - started) * 1000),
                          **assess_memory_representations(case, representations, recall=index in ranks[:2]),
                          "lexical_top2_recall": index in ranks[:2],
                          "representations": representations, "calls": calls[start_call:]}
                records.append(record)
                print(json.dumps({key: value for key, value in record.items() if key != "representations"}), file=sys.stderr, flush=True)
    finally:
        if previous_mode is None:
            os.environ.pop("MEM0_QUERY_EXPANSION_MODE", None)
        else:
            os.environ["MEM0_QUERY_EXPANSION_MODE"] = previous_mode
        await client.close()
    return {"started_utc": started_utc, "synthetic": True, "request_ceiling": 36,
            "quality_version": 2, "requests_attempted": requests_attempted, "cases": CASES,
            "quality_scope": "Required terms, synthetic lexical retrieval, and targeted unsupported-hypothesis detection; not semantic production recall accuracy.",
            "results": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for billable synthetic requests")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

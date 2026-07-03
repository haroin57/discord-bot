#!/usr/bin/env python3
"""P0.5-c: fixed-query recall regression for the live Mem0 store.

Search-quality regressions don't show up in unit tests (plan review, all four
models agreed). This harness runs a fixed query set against the REAL store
and compares which memory ids come back, so a refactor of the search path
(P5-a) can prove "same inputs → same memories".

Determinism: uses plain vector + hybrid search only (no LLM rewrite / HyDE /
rerank — those are non-deterministic and would make every diff noise). The
embedding of a fixed query string is stable for a fixed embedder model.

Usage:
    .venv/bin/python scripts/eval_mem0_recall_fixed.py --baseline   # record
    .venv/bin/python scripts/eval_mem0_recall_fixed.py --compare    # gate
Exit codes for --compare: 0 = within tolerance, 1 = recall drift breach.
"""

import argparse
import asyncio
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_ROOT, ".env"))

BASELINE_PATH = os.path.join(_ROOT, "tests", "fixtures", "mem0_eval", "recall_baseline.json")
DEFAULT_USER_ID = "573489206240870410"  # 先輩
TOP_K = 10
# Jaccard overlap per query must stay >= this against the baseline id set.
MIN_OVERLAP = 0.7

# 30 fixed queries spanning the store's main themes (personal / tech / music /
# invest / ops). Editing this list invalidates the baseline — re-record it.
QUERIES = [
    "先輩の大学と学部", "先輩の出身地", "先輩の家族構成", "先輩の健康状態",
    "先輩の好きな音楽", "先輩の好きなアニメ", "先輩の酒とタバコの好み",
    "先輩の進学希望", "先輩のインターン先", "先輩のプログラミング歴",
    "ウィスキーの好み", "UK Garage", "米津玄師", "留学の計画",
    "discord botの構成", "mem0の設計", "claude apiの移行", "デプロイの手順",
    "テストの方針", "リファクタリングの計画", "oauth failover",
    "投資のポートフォリオ", "VIストラテジー", "日経平均", "ドル円",
    "お父さんとの会話", "b0nchichi", "かなえの人格設定",
    "早寝早起きのルール", "深夜の作業習慣",
]


async def run_queries() -> dict:
    from services.mem0 import search_memories
    from services.mem0.search import search_memories_hybrid

    results: dict[str, dict] = {}
    for q in QUERIES:
        plain = await search_memories(q, DEFAULT_USER_ID, limit=TOP_K)
        try:
            hybrid = await search_memories_hybrid(q, DEFAULT_USER_ID, limit=TOP_K)
        except Exception as exc:  # noqa: BLE001 — hybrid backend optional
            print(f"  [warn] hybrid failed for {q!r}: {exc}")
            hybrid = []
        results[q] = {
            "plain_ids": [r.get("id") for r in plain if r.get("id")],
            "hybrid_ids": [r.get("id") for r in hybrid if r.get("id")],
        }
        print(f"  {q}: plain={len(results[q]['plain_ids'])} hybrid={len(results[q]['hybrid_ids'])}")
    return results


def _overlap(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 1.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true", help="record baseline")
    mode.add_argument("--compare", action="store_true", help="gate against baseline")
    args = ap.parse_args()

    print(f"[recall-eval] running {len(QUERIES)} fixed queries (top-{TOP_K})")
    current = await run_queries()

    if args.baseline:
        os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=1)
        print(f"[recall-eval] baseline written: {BASELINE_PATH}")
        return 0

    if not os.path.isfile(BASELINE_PATH):
        print("[recall-eval] no baseline — run --baseline first")
        return 2
    with open(BASELINE_PATH, encoding="utf-8") as f:
        baseline = json.load(f)

    breaches = []
    for q, base in baseline.items():
        cur = current.get(q, {})
        for kind in ("plain_ids", "hybrid_ids"):
            ov = _overlap(base.get(kind, []), cur.get(kind, []))
            if ov < MIN_OVERLAP:
                breaches.append((q, kind, ov))
    if breaches:
        print(f"[recall-eval] BREACH — {len(breaches)} query/kind pairs under {MIN_OVERLAP}:")
        for q, kind, ov in breaches[:10]:
            print(f"  {q} [{kind}]: overlap={ov:.2f}")
        return 1
    print(f"[recall-eval] OK — all {len(baseline)} queries within tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

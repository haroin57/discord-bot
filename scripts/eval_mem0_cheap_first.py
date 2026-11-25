#!/usr/bin/env python3
"""Offline recall eval for Mem0 cheap-first escalation (explore-C #2).

Drives the real ``services.mem0.search.search_memories_smart`` orchestration
with a deterministic fake retriever (NO LLM, NO network, NO Chroma, NO billing)
under the cheap-first flag OFF vs ON, and reports, per query class:

  * recall  = |gold ∩ returned| / |gold|   (averaged over the class)
  * helper  = rewrite + HyDE + keywords + rerank LLM calls per query

The flag defaults OFF, so this is purely an enable-decision aid: it quantifies
how many helper LLM round-trips cheap-first removes and whether recall holds.

Model (single source of truth = cosine distance):
  * The cheap tier (alias / morpho / hybrid vector+BM25) retrieves a query's
    ``cheap_gold`` at ``cheap_best_distance`` plus fixed distractors.
  * The expensive tier's helper variants additionally surface ``helper_gold``
    (facts only reachable through rewrite / HyDE / keyword expansion).
  * Escalation is the shipped deterministic decision
    (``should_escalate_recall``): escalate iff best cheap distance ≥ threshold.

This is a STRUCTURAL eval: the aggregate recall depends on the (explicit,
illustrative) class mix below, not on production traffic. Read the per-class
rows — they are mix-independent.

Usage:
    .venv/bin/python scripts/eval_mem0_cheap_first.py [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_PATCH = "services.mem0.search"

# Fixed distances. CONFIDENT < 0.75 escalate threshold; WEAK ≥ it.
_DIST_DISTRACTOR = 1.0
_DIST_HELPER_GOLD = 0.45

# Illustrative class mix (weights sum need not be 1; normalised below). Chosen
# to span the behaviour space, NOT to model production frequencies.
SCENARIOS = [
    # id, class, cheap_best_distance, gold, cheap_gold, helper_gold, weight
    ("single_confident", "single_confident", 0.20, ["g1"], ["g1"], [], 3),
    ("single_weak", "single_weak", 0.90, ["g1"], [], ["g1"], 2),
    ("multi_confident", "multi_confident", 0.20,
     ["g1", "g2", "g3", "g4", "g5", "g6"], ["g1"],
     ["g2", "g3", "g4", "g5", "g6"], 2),
    ("multi_weak", "multi_weak", 0.85,
     ["g1", "g2", "g3", "g4"], [], ["g1", "g2", "g3", "g4"], 1),
    ("no_memory", "no_memory", 1.30, [], [], [], 1),
]


def _async_const(value):
    async def _f(*a, **k):
        return value
    return _f


class _Harness:
    def __init__(self, sc):
        (_id, _cls, self.cheap_best, self.gold,
         self.cheap_gold, self.helper_gold, _w) = sc
        self.rewrite = self.hyde = self.keywords = self.rerank = 0

    def _cheap_pool(self):
        pool = [{"id": g, "memory": g, "score": self.cheap_best}
                for g in self.cheap_gold]
        pool += [{"id": "d1", "memory": "distract", "score": _DIST_DISTRACTOR},
                 {"id": "d2", "memory": "distract", "score": _DIST_DISTRACTOR}]
        return pool

    def _full_pool(self):
        pool = self._cheap_pool()
        pool += [{"id": g, "memory": g, "score": _DIST_HELPER_GOLD}
                 for g in self.helper_gold]
        return pool

    def install(self, monkeypatch_env):
        async def _rewrite(q):
            self.rewrite += 1
            return ["rw"]

        async def _hyde(q):
            self.hyde += 1
            return "hyde"

        async def _keywords(q):
            self.keywords += 1
            return "kw"

        async def _rerank(query, candidates, top_k):
            self.rerank += 1
            ordered = sorted(candidates, key=lambda r: r.get("score") or 9.0)
            return ordered[:top_k]

        async def _multi(queries, user_id, *, limit, apply_distance_filter,
                         current_channel_id=None):
            expensive = any(q in ("rw", "hyde", "kw") for q in queries)
            return self._full_pool() if expensive else self._cheap_pool()

        async def _graph_identity(cands, user_id, *, current_channel_id=None):
            return cands

        patches = {
            "_expand_query_via_graph": _async_const([]),
            "_expand_query_via_morpho": lambda q: "",
            "_prewarm_query_embeddings": _async_const(None),
            "_rewrite_query": _rewrite,
            "_generate_hypothetical_fact": _hyde,
            "_extract_keywords_via_llm": _keywords,
            "_rerank_memories": _rerank,
            "_search_multi_hybrid": _multi,
            "_graph_expand_candidates": _graph_identity,
            "_apply_reinforcement_boost": lambda c: None,
            "_fire_recall_reinforcement": lambda c: None,
            "_expand_episode_bundle": _async_const(0),
        }
        self._ctx = [mock.patch(f"{_PATCH}.{name}", new) for name, new in patches.items()]
        for c in self._ctx:
            c.start()

    def uninstall(self):
        for c in self._ctx:
            c.stop()


async def _run_one(sc, enabled: bool) -> dict:
    import config
    from services.mem0.search import search_memories_smart

    h = _Harness(sc)
    h.install(None)
    try:
        with mock.patch.object(config, "MEM0_CHEAP_FIRST_ENABLED", enabled):
            result = await search_memories_smart("先輩についての質問", "u", limit=5)
    finally:
        h.uninstall()
    returned = {r.get("id") for r in result if r.get("id")}
    gold = set(h.gold)
    recall = 1.0 if not gold else len(gold & returned) / len(gold)
    helpers = h.rewrite + h.hyde + h.keywords + h.rerank
    return {"recall": recall, "helpers": helpers, "weight": sc[6]}


async def main(as_json: bool) -> int:
    rows = []
    for sc in SCENARIOS:
        off = await _run_one(sc, enabled=False)
        on = await _run_one(sc, enabled=True)
        rows.append({
            "id": sc[0], "class": sc[1], "weight": sc[6],
            "recall_off": off["recall"], "recall_on": on["recall"],
            "helpers_off": off["helpers"], "helpers_on": on["helpers"],
        })

    wsum = sum(r["weight"] for r in rows)
    agg = {
        "recall_off": sum(r["recall_off"] * r["weight"] for r in rows) / wsum,
        "recall_on": sum(r["recall_on"] * r["weight"] for r in rows) / wsum,
        "helpers_off": sum(r["helpers_off"] * r["weight"] for r in rows) / wsum,
        "helpers_on": sum(r["helpers_on"] * r["weight"] for r in rows) / wsum,
    }
    agg["helper_reduction_pct"] = (
        100.0 * (agg["helpers_off"] - agg["helpers_on"]) / agg["helpers_off"]
        if agg["helpers_off"] else 0.0
    )
    agg["recall_delta"] = agg["recall_on"] - agg["recall_off"]

    if as_json:
        print(json.dumps({"rows": rows, "aggregate": agg}, ensure_ascii=False, indent=2))
        return 0

    print("\n=== Mem0 cheap-first offline recall eval (flag OFF vs ON) ===")
    print(f"{'class':20s} {'w':>2s} {'recall_off':>10s} {'recall_on':>9s} "
          f"{'help_off':>8s} {'help_on':>7s}")
    for r in rows:
        print(f"{r['class']:20s} {r['weight']:>2d} "
              f"{r['recall_off']:>10.2f} {r['recall_on']:>9.2f} "
              f"{r['helpers_off']:>8d} {r['helpers_on']:>7d}")
    print("-" * 62)
    print(f"{'WEIGHTED AVG':20s} {'':>2s} "
          f"{agg['recall_off']:>10.3f} {agg['recall_on']:>9.3f} "
          f"{agg['helpers_off']:>8.2f} {agg['helpers_on']:>7.2f}")
    print(f"\nhelper-call reduction: {agg['helper_reduction_pct']:.1f}%  "
          f"(weighted avg {agg['helpers_off']:.2f} -> {agg['helpers_on']:.2f} LLM calls/query)")
    print(f"recall delta (ON - OFF): {agg['recall_delta']:+.3f}  "
          f"(ON {'>=' if agg['recall_delta'] >= 0 else '<'} OFF)")
    print("\nPer-class reading:")
    print("  single_confident : cheap top hit is close; helpers add nothing → skip, recall held")
    print("  single_weak      : cheap miss → escalate → full recall recovered")
    print("  multi_confident  : close top-1 masks 5 helper-only facts → RECALL RISK on skip")
    print("  multi_weak       : cheap miss → escalate → full recall recovered")
    print("  no_memory        : nothing relevant either way")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mem0 cheap-first offline recall eval")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.json)))

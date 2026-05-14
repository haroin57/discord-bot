#!/usr/bin/env python3
"""Retroactively apply Hebbian reinforcement to the existing Mem0 corpus.

For every pair of facts with cosine ≥ DEDUP_GRAY_LOW, ask Sonnet whether
they describe the same fact. Pairs that the LLM judges identical form a
cluster. Within each cluster:

  1. Pick the "keep" fact (longest text + highest importance).
  2. Sum the mention_count across the cluster onto the keep fact.
  3. Merge reinforced_at history (most-recent N entries).
  4. Bump importance to min(max(cluster) + 1, 5).
  5. Delete every non-keep fact in the cluster.

Run on a quiet bot (stop discord-bot first) so concurrent ingest doesn't
fight the writes. Idempotent — re-running on an already-collapsed corpus
finds no new pairs.

Usage:
    .venv/bin/python scripts/mem0_reinforce_existing.py --dry-run --user-id 573489206240870410
    .venv/bin/python scripts/mem0_reinforce_existing.py --user-id 573489206240870410
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mem0_reinforce")

DEFAULT_USER = "573489206240870410"
PAIR_THRESHOLD = 0.80  # mirrors services/mem0/dedup.py:DEDUP_GRAY_LOW


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _normalize(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    norm[norm == 0] = 1.0
    return v / norm


async def collect_pairs(
    facts: list[dict], embeddings: np.ndarray, threshold: float,
) -> list[tuple[int, int, float]]:
    """Return [(i, j, score)] for every i<j with cosine ≥ threshold and
    candidate-category compatible (skip when categories differ)."""
    norm = _normalize(embeddings)
    sims = norm @ norm.T
    n = len(facts)
    pairs: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sims[i, j])
            if s < threshold:
                continue
            ci = (facts[i].get("metadata") or {}).get("category")
            cj = (facts[j].get("metadata") or {}).get("category")
            if ci and cj and ci != cj:
                continue
            pairs.append((i, j, s))
    pairs.sort(key=lambda x: -x[2])
    return pairs


async def llm_same_fact(text_a: str, text_b: str) -> bool:
    """Call services.mem0.dedup.llm_judge_same_fact."""
    from services.mem0.dedup import llm_judge_same_fact
    return await llm_judge_same_fact(text_a, text_b)


def _meta_int(meta: dict, key: str, default: int) -> int:
    try:
        return int(meta.get(key, default))
    except (TypeError, ValueError):
        return default


async def run(args):
    from services.mem0.client import _get_mem0
    mem = _get_mem0()
    if mem is None:
        raise SystemExit("Mem0 not initialized")

    log.info("loading mem0 facts for user=%s", args.user_id)
    raw = await asyncio.to_thread(mem.get_all, user_id=args.user_id, limit=20000)
    facts = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    log.info("loaded %d facts", len(facts))
    if len(facts) < 2:
        log.info("nothing to do — fewer than 2 facts")
        return

    from openai import OpenAI
    oai = OpenAI()

    # Embed all fact texts.
    texts = [(f.get("memory") or "").strip() or "(empty)" for f in facts]
    BATCH = 100
    chunks: list[np.ndarray] = []
    for i in range(0, len(texts), BATCH):
        b = texts[i : i + BATCH]
        resp = oai.embeddings.create(model="text-embedding-3-small", input=b)
        chunks.append(np.array([e.embedding for e in resp.data], dtype=np.float32))
        log.info("embedded %d/%d", min(i + BATCH, len(texts)), len(texts))
    matrix = np.concatenate(chunks, axis=0)

    pairs = await collect_pairs(facts, matrix, args.threshold)
    log.info("candidate pairs (cosine ≥ %.2f): %d", args.threshold, len(pairs))
    if not pairs:
        return

    # LLM-judge each pair (bounded concurrency).
    sem = asyncio.Semaphore(args.max_concurrent)

    async def _judge(i: int, j: int, score: float) -> tuple[int, int, bool, float]:
        async with sem:
            ok = await llm_same_fact(texts[i], texts[j])
            return (i, j, ok, score)

    log.info("running %d LLM judges (concurrency=%d)…", len(pairs), args.max_concurrent)
    judged: list[tuple[int, int, bool, float]] = []
    for chunk_start in range(0, len(pairs), 50):
        chunk = pairs[chunk_start : chunk_start + 50]
        results = await asyncio.gather(*(_judge(i, j, s) for i, j, s in chunk))
        judged.extend(results)
        log.info("judged %d/%d", min(chunk_start + 50, len(pairs)), len(pairs))

    same_pairs = [(i, j, s) for i, j, ok, s in judged if ok]
    log.info("same-fact pairs: %d / %d", len(same_pairs), len(judged))
    if not same_pairs:
        return

    # Build clusters via union-find.
    uf = UnionFind(len(facts))
    for i, j, _ in same_pairs:
        uf.union(i, j)
    clusters: dict[int, list[int]] = {}
    for idx in range(len(facts)):
        if any(idx == i or idx == j for i, j, _ in same_pairs):
            clusters.setdefault(uf.find(idx), []).append(idx)
    clusters = {root: members for root, members in clusters.items() if len(members) > 1}
    log.info("non-trivial clusters: %d", len(clusters))

    merged = deleted = errors = 0
    for root, members in clusters.items():
        # Pick keep = longest text, importance tiebreak.
        keep_idx = max(
            members,
            key=lambda x: (
                len((facts[x].get("memory") or "")),
                _meta_int(facts[x].get("metadata") or {}, "importance", 3),
            ),
        )
        keep = facts[keep_idx]
        keep_meta = keep.get("metadata") or {}

        total_mentions = sum(
            _meta_int(facts[m].get("metadata") or {}, "mention_count", 1)
            for m in members
        )
        history: set[int] = set()
        for m in members:
            for ts in (facts[m].get("metadata") or {}).get("reinforced_at") or []:
                try:
                    history.add(int(ts))
                except (TypeError, ValueError):
                    pass
        history.add(int(time.time()))  # this consolidation counts as a reinforcement
        history_list = sorted(history)[-10:]
        max_imp = max(
            _meta_int(facts[m].get("metadata") or {}, "importance", 3) for m in members
        )

        partial = {
            "mention_count": total_mentions,
            "reinforced_at": history_list,
            "last_reinforced_at": history_list[-1] if history_list else int(time.time()),
            "importance": min(max_imp + 1, 5),
        }

        sample = ", ".join(
            f"{facts[m].get('id','?')[:8]}({_meta_int(facts[m].get('metadata') or {}, 'mention_count', 1)})"
            for m in members[:6]
        )
        log.info(
            "cluster (%d members, keep=%s, mention_count→%d): %s",
            len(members), keep.get("id", "?")[:8], total_mentions, sample,
        )

        if args.dry_run:
            continue

        # Update keep.
        try:
            await asyncio.to_thread(
                mem.update,
                memory_id=keep["id"],
                data=keep.get("memory") or "",
                metadata=partial,
            )
            merged += 1
        except Exception as exc:
            log.warning("update keep %s failed: %s", keep["id"], exc)
            errors += 1
            continue

        # Delete other members.
        for m in members:
            if m == keep_idx:
                continue
            try:
                await asyncio.to_thread(mem.delete, facts[m]["id"])
                deleted += 1
            except Exception as exc:
                log.warning("delete %s failed: %s", facts[m]["id"], exc)
                errors += 1

    log.info("=== summary ===")
    log.info("clusters processed: %d", len(clusters))
    log.info("keep facts updated: %d", merged)
    log.info("duplicate facts deleted: %d", deleted)
    log.info("errors: %d", errors)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", default=DEFAULT_USER)
    p.add_argument("--threshold", type=float, default=PAIR_THRESHOLD)
    p.add_argument("--max-concurrent", type=int, default=4)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

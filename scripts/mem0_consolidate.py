#!/usr/bin/env python3
"""Offline Mem0 cluster consolidation (止揚) — ADR-0016.

Ingest-time dedup (services/mem0/dedup.py) only compares a new fact against
its top-5 nearest neighbours, so related facts that accumulated across
different conversations / queries over time are never consolidated. This
batch finds clusters of such facts (vector greedy clustering), asks the LLM
to consolidate each cluster into fewer richer facts (synthesize_cluster —
the N-way generalisation of the aufhebung merge), inserts the consolidated
facts, and SOFT-ARCHIVES the originals (valid_to + consolidated_into).

Empirically validated on the live store (2026-06-15): sim>=0.83 yields
clean clusters with no over-merge; a 7-fact CLANNAD temporal cluster
collapses to the single latest-state fact.

DRY-RUN by default (MEM0_CONSOLIDATE_DRY_RUN): only writes a markdown report
to /tmp/mem0_consolidate_<date>.md, no store mutation. Flip the env to 0 to
apply. Reversible: originals are soft-archived, not deleted
(MEM0_HIDE_ARCHIVED=0 restores them).

Usage:
    .venv/bin/python scripts/mem0_consolidate.py --user-id 573489206240870410
    MEM0_CONSOLIDATE_DRY_RUN=0 .venv/bin/python scripts/mem0_consolidate.py --user-id ...
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime
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
log = logging.getLogger("mem0_consolidate")

DEFAULT_USER = "573489206240870410"


def _normalize(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    norm[norm == 0] = 1.0
    return v / norm


def _meta_int(meta: dict, key: str, default: int) -> int:
    try:
        return int(meta.get(key, default))
    except (TypeError, ValueError):
        return default


def _is_archived(meta: dict) -> bool:
    """Skip already soft-archived facts (valid_to in the past) and facts that
    were already consolidated (idempotency)."""
    if meta.get("consolidated_into"):
        return True
    vt = meta.get("valid_to")
    if vt is None:
        return False
    try:
        return int(vt) <= int(time.time())
    except (TypeError, ValueError):
        return False


def greedy_clusters(facts, embeddings, sim_threshold, max_cluster):
    """Greedy cosine clustering, category-aware. Returns list of index lists
    (each of size 2..max_cluster). Mirrors the validated POC algorithm."""
    En = _normalize(embeddings)
    n = len(facts)
    assigned = [-1] * n
    clusters: list[list[int]] = []
    cats = [(f.get("metadata") or {}).get("category") for f in facts]
    for i in range(n):
        if assigned[i] != -1:
            continue
        sims = En @ En[i]
        members = []
        for j in range(n):
            if assigned[j] != -1 or sims[j] < sim_threshold:
                continue
            # category filter: don't cluster across distinct categories
            if cats[i] and cats[j] and cats[i] != cats[j]:
                continue
            members.append(j)
            if len(members) >= max_cluster:
                break
        cid = len(clusters)
        for j in members:
            assigned[j] = cid
        clusters.append(members)
    return [c for c in clusters if len(c) >= 2]


def _merge_metadata(cluster_facts: list[dict]) -> dict:
    """Derive the consolidated fact's metadata from its cluster members."""
    metas = [f.get("metadata") or {} for f in cluster_facts]
    importance = max((_meta_int(m, "importance", 3) for m in metas), default=3)
    mention = sum(_meta_int(m, "mention_count", 1) for m in metas)
    cats = [m.get("category") for m in metas if m.get("category")]
    category = Counter(cats).most_common(1)[0][0] if cats else "meta"
    tier = 2 if importance <= 2 else 1
    return {
        "category": str(category),
        "importance": importance,
        "tier": tier,
        "mention_count": mention,
        "source": "consolidate",
        "consolidated_from": len(cluster_facts),
        "consolidated_at": int(time.time()),
    }


async def run(args) -> int:
    from config import (
        MEM0_CONSOLIDATE_SIM,
        MEM0_CONSOLIDATE_DRY_RUN,
        MEM0_CONSOLIDATE_MAX_CLUSTER,
        MEM0_CONSOLIDATE_MAX_CLUSTERS,
        MEM0_CONSOLIDATE_GATE,
    )
    from services.mem0.client import _get_mem0
    from services.mem0.dedup import synthesize_cluster, partition_mergeable

    dry_run = MEM0_CONSOLIDATE_DRY_RUN if args.apply is None else (not args.apply)
    sim = args.sim or MEM0_CONSOLIDATE_SIM

    mem = _get_mem0()
    if mem is None:
        raise SystemExit("Mem0 not initialized")

    log.info("loading facts for user=%s (dry_run=%s sim=%.2f)",
             args.user_id, dry_run, sim)
    raw = await asyncio.to_thread(mem.get_all, user_id=args.user_id, limit=20000)
    all_facts = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    # Drop archived / already-consolidated (idempotency).
    facts = [
        f for f in all_facts
        if (f.get("memory") or "").strip()
        and not _is_archived(f.get("metadata") or {})
    ]
    log.info("loaded %d facts (%d after archived/consolidated filter)",
             len(all_facts), len(facts))
    if len(facts) < 2:
        log.info("nothing to do")
        return 0

    report_lines = [
        f"# Mem0 consolidation report ({datetime.now():%Y-%m-%d %H:%M})",
        f"- user_id: {args.user_id}",
        f"- mode: {'DRY-RUN (no writes)' if dry_run else 'APPLY'}",
        "",
    ]
    stats = {"hash_dup_groups": 0, "hash_archived": 0,
             "clusters_processed": 0, "consolidated_in": 0,
             "consolidated_out": 0, "archived": 0, "skipped": 0, "errors": 0}

    now0 = int(time.time())

    # --- Phase 0: deterministic exact-dup sweep by metadata['hash'] (P1) ---
    # Every fact carries a content hash; exact dups share it. Collapse each
    # hash group to one representative (highest mention_count), reinforce it
    # with the summed mention_count, and soft-archive the rest. No embeddings,
    # no LLM. Catches the parallel-ingest race dups deterministically.
    by_hash: dict[str, list[int]] = {}
    for i, f in enumerate(facts):
        # mem.get_all exposes the content hash at the TOP level (not metadata).
        h = f.get("hash")
        if h:
            by_hash.setdefault(h, []).append(i)
    hash_archived_idx: set[int] = set()
    report_lines.append("## phase 0: exact-dup hash sweep")
    for h, idxs in by_hash.items():
        if len(idxs) < 2:
            continue
        stats["hash_dup_groups"] += 1
        # representative = highest mention_count, then longest text
        rep = max(idxs, key=lambda i: (
            _meta_int(facts[i].get("metadata") or {}, "mention_count", 1),
            len(facts[i].get("memory") or ""),
        ))
        dups = [i for i in idxs if i != rep]
        report_lines.append(
            f"- hash {h[:8]} ×{len(idxs)} → keep 1: {(facts[rep].get('memory') or '')[:60]}"
        )
        if dry_run:
            continue
        try:
            from services.mem0.reinforce import reinforce_existing
            # reinforce representative once per absorbed dup (mention_count++)
            for _ in dups:
                await reinforce_existing(mem, facts[rep].get("id"))
        except Exception as exc:
            log.warning("hash-sweep reinforce failed: %s", exc)
        for i in dups:
            oid = facts[i].get("id")
            if not oid:
                continue
            try:
                await asyncio.to_thread(
                    mem.update, memory_id=oid, data=facts[i].get("memory") or "",
                    metadata={"valid_to": now0,
                              "conflict_archived_as": "exact_dup",
                              "consolidated_into": facts[rep].get("id")},
                )
                try:
                    from services.mem0 import bm25_index
                    bm25_index.remove_fact(oid)
                except Exception:
                    pass
                stats["hash_archived"] += 1
                hash_archived_idx.add(i)
            except Exception as exc:
                log.warning("hash-sweep archive %s failed: %s", oid, exc)
                stats["errors"] += 1
    report_lines.append("")

    # Drop hash-archived (when applying) so the vector phase ignores them.
    if not dry_run and hash_archived_idx:
        facts = [f for i, f in enumerate(facts) if i not in hash_archived_idx]

    if getattr(args, "hash_only", False):
        report_lines.append("\n## stats")
        for k, v in stats.items():
            report_lines.append(f"- {k}: {v}")
        out = args.out or f"/tmp/mem0_consolidate_{datetime.now():%Y%m%d_%H%M}.md"
        Path(out).write_text("\n".join(report_lines), encoding="utf-8")
        os.chmod(out, 0o600)
        log.info("hash-only report → %s  stats=%s", out, stats)
        return 0

    # Embed (OpenAI batches) — same approach as mem0_reinforce_existing.
    from openai import OpenAI
    oai = OpenAI()
    texts = [(f.get("memory") or "").strip() for f in facts]
    chunks = []
    BATCH = 256
    for i in range(0, len(texts), BATCH):
        resp = oai.embeddings.create(
            model="text-embedding-3-small", input=texts[i:i + BATCH]
        )
        chunks.append(np.array([e.embedding for e in resp.data], dtype=np.float32))
        log.info("embedded %d/%d", min(i + BATCH, len(texts)), len(texts))
    matrix = np.concatenate(chunks, axis=0)

    # texts list must track the (possibly hash-pruned) facts list.
    texts = [(f.get("memory") or "").strip() for f in facts]
    clusters = greedy_clusters(facts, matrix, sim, MEM0_CONSOLIDATE_MAX_CLUSTER)
    log.info("clusters (sim>=%.2f): %d multi-fact, %d facts absorbed",
             sim, len(clusters), sum(len(c) for c in clusters))
    report_lines.append(
        f"## phase 1: vector consolidation (sim>={sim}, gate={MEM0_CONSOLIDATE_GATE})\n"
        f"clusters={len(clusters)} absorbed={sum(len(c) for c in clusters)} "
        f"facts={len(facts)}\n"
    )

    # P3 gate: split each vector cluster into pairwise-mergeable sub-clusters
    # so distinct aspects accidentally co-clustered are never fused. Then
    # process each size>=2 sub-cluster as a consolidation unit.
    groups: list[list[int]] = []
    for c in clusters:
        if MEM0_CONSOLIDATE_GATE and len(c) >= 2:
            parts = await partition_mergeable([texts[i] for i in c])
            for part in parts:
                sub = [c[k] for k in part]
                if len(sub) >= 2:
                    groups.append(sub)
        else:
            groups.append(c)

    processed = 0
    for c in sorted(groups, key=len, reverse=True):
        if processed >= MEM0_CONSOLIDATE_MAX_CLUSTERS:
            log.info("cluster cap %d reached — stopping", MEM0_CONSOLIDATE_MAX_CLUSTERS)
            report_lines.append(
                f"\n_(cluster cap {MEM0_CONSOLIDATE_MAX_CLUSTERS} reached; "
                f"{len(groups) - processed} groups not processed this run)_"
            )
            break
        cluster_facts = [facts[i] for i in c]
        cluster_texts = [texts[i] for i in c]
        # Exact-dup fast path: when every member is byte-identical there is
        # nothing for the LLM to synthesize — keep the text deterministically
        # and skip the Haiku call. ~38% of clusters on the live store are
        # exact dups (parallel re-ingest races), so this is a large saving and
        # removes any LLM-rewording risk on identical input.
        uniq = list(dict.fromkeys(t.strip() for t in cluster_texts))
        if len(uniq) == 1:
            consolidated = [uniq[0]]
            stats["exact_dup"] = stats.get("exact_dup", 0) + 1
        else:
            consolidated = await synthesize_cluster(cluster_texts)
        if not consolidated:
            stats["skipped"] += 1
            continue
        processed += 1
        stats["clusters_processed"] += 1
        stats["consolidated_in"] += len(cluster_texts)
        stats["consolidated_out"] += len(consolidated)

        report_lines.append(f"## cluster size {len(c)} → {len(consolidated)}")
        report_lines.append("**before:**")
        for t in cluster_texts:
            report_lines.append(f"- {t}")
        report_lines.append("**after:**")
        for t in consolidated:
            report_lines.append(f"- ✅ {t}")
        report_lines.append("")

        if dry_run:
            continue

        # APPLY: insert consolidated facts, soft-archive originals.
        try:
            base_meta = _merge_metadata(cluster_facts)
            new_ids: list[str] = []
            for new_text in consolidated:
                res = await asyncio.to_thread(
                    mem.add,
                    [{"role": "user", "content": new_text}],
                    user_id=args.user_id, infer=False, metadata=dict(base_meta),
                )
                # bm25 index the new fact
                fid = None
                if isinstance(res, dict):
                    for r in (res.get("results") or []):
                        if isinstance(r, dict) and r.get("id"):
                            fid = r["id"]; new_ids.append(fid)
                            try:
                                from services.mem0 import bm25_index
                                bm25_index.index_fact(fid, new_text, args.user_id)
                            except Exception as exc:
                                log.warning("bm25 index_fact failed: %s", exc)
                stats["consolidated_out"] = stats["consolidated_out"]  # noqa
            into = new_ids[0] if new_ids else "?"
            now = int(time.time())
            for f in cluster_facts:
                oid = f.get("id")
                if not oid:
                    continue
                try:
                    await asyncio.to_thread(
                        mem.update, memory_id=oid,
                        data=f.get("memory") or "",
                        metadata={
                            "valid_to": now,
                            "conflict_archived_as": "consolidated",
                            "consolidated_into": into,
                        },
                    )
                    try:
                        from services.mem0 import bm25_index
                        bm25_index.remove_fact(oid)
                    except Exception as exc:
                        log.warning("bm25 remove_fact failed: %s", exc)
                    stats["archived"] += 1
                except Exception as exc:
                    log.warning("archive %s failed: %s", oid, exc)
                    stats["errors"] += 1
        except Exception as exc:
            log.warning("apply cluster failed: %s", exc)
            stats["errors"] += 1

    report_lines.append("\n## stats")
    for k, v in stats.items():
        report_lines.append(f"- {k}: {v}")
    report = "\n".join(report_lines)

    out = args.out or f"/tmp/mem0_consolidate_{datetime.now():%Y%m%d_%H%M}.md"
    Path(out).write_text(report, encoding="utf-8")
    os.chmod(out, 0o600)
    log.info("report → %s", out)
    log.info("stats: %s", stats)
    return 0


def main():
    p = argparse.ArgumentParser(description="Offline Mem0 cluster consolidation")
    p.add_argument("--user-id", default=DEFAULT_USER)
    p.add_argument("--sim", type=float, default=None, help="cosine threshold override")
    p.add_argument("--apply", dest="apply", action="store_true", default=None,
                   help="apply writes (overrides MEM0_CONSOLIDATE_DRY_RUN)")
    p.add_argument("--hash-only", dest="hash_only", action="store_true",
                   default=False, help="run only the exact-dup hash sweep (P1)")
    p.add_argument("--dry-run", dest="apply", action="store_false",
                   help="force dry-run")
    p.add_argument("--out", default=None, help="report path")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()

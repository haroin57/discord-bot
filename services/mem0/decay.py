"""Forgetting — Phase ②'s counterpart to reinforce.py's strengthening.

Before this module, ``tier`` (Live/Hot/Warm/Cold — see extract.py's
classification prompts) was assigned once at ingest time and never
revisited. A fact ingested as Hot stayed Hot forever, even if it was never
mentioned or recalled again, and there was no path from "rarely touched
fact" to "compressed/archived" at all. That's the human-memory-inspired
plan's ②: ACT-R base-level activation (reinforce.py's ``_actr_activation``)
already tells us how "available" a fact currently is — this module turns
that into two operations a caller can run periodically (see
``scripts/mem0_tier_decay.py``):

  1. **Tier decay** (``recompute_tiers_for_user``): reclassify every active
     fact's tier from its current activation. This is what lets a fact
     naturally fall out of ``search.get_core_memories`` (imp>=5 AND tier in
     {Live, Hot}) once it stops being reinforced or recalled — mirroring how
     a human's core beliefs that go unrehearsed for years drift out of
     constant working recall even though they're still "known".

  2. **Gist compression** (``gist_compress_for_user``): fuzzy-trace-theory
     -style forgetting for facts that have decayed to Cold — cluster them by
     shared entity and collapse each cluster into fewer, detail-poorer
     "gist" facts via ``dedup.synthesize_cluster``. Originals are
     soft-archived (``valid_to``), never deleted, so
     ``MEM0_HIDE_ARCHIVED=false`` always restores the verbatim originals.

Not to be confused with ``scripts/mem0_consolidate.py`` (ADR-0016) — that
system clusters facts by EMBEDDING similarity to merge near-duplicate
wording regardless of age. This module clusters Cold-tier facts by SHARED
ENTITY to compress genuinely old/rarely-touched material into a summary.
The two are complementary passes over different criteria, not competing
implementations of the same idea.

Both are dry-run by default (see config.py) and are pure additions: no
existing ingest/search code path depends on either running.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)

# --- Tier thresholds, expressed directly in ACT-R activation units ---------
# Activation is ln(sum of decayed presentation weights) and, with the decay
# exponent d=0.5, falls off MUCH faster than calendar intuition suggests: a
# fact mentioned exactly once decays as -0.5*ln(seconds_since), which is
# already -5.68 after just one day and -8.63 after a year. These cutoffs are
# calibrated against that single-presentation curve (each reinforcement/
# recall pushes activation back up, which is what lets a frequently-touched
# fact stay Live/Hot far longer than a single mention would):
#   >= -6.0  Live  (single mention within ~a few days, or anything reinforced recently)
#   >= -7.5  Hot   (single mention within ~a month)
#   >= -8.7  Warm  (single mention within ~a year)
#   <  -8.7  Cold
# Tune via env if a corpus's actual distribution (see scripts/mem0_tier_decay.py
# --dry-run output) skews from these assumptions.
_TIER_LIVE_MIN = float(os.environ.get("MEM0_TIER_LIVE_ACTIVATION", "-6.0"))
_TIER_HOT_MIN = float(os.environ.get("MEM0_TIER_HOT_ACTIVATION", "-7.5"))
_TIER_WARM_MIN = float(os.environ.get("MEM0_TIER_WARM_ACTIVATION", "-8.7"))
# activation < _TIER_WARM_MIN -> Cold (3)

GIST_MIN_CLUSTER_SIZE = int(os.environ.get("MEM0_GIST_MIN_CLUSTER_SIZE", "3"))
# Facts at or below this importance are eligible for gist compression;
# imp > this is exempt (a fact the extractor judged genuinely load-bearing
# must never be silently reduced to a lossy summary).
GIST_MAX_IMPORTANCE = int(os.environ.get("MEM0_GIST_MAX_IMPORTANCE", "3"))
GIST_MAX_CLUSTERS_PER_RUN = int(os.environ.get("MEM0_GIST_MAX_CLUSTERS_PER_RUN", "20"))


def activation_to_tier(activation: float) -> int:
    """Map an ACT-R activation value to a tier: 0=Live, 1=Hot, 2=Warm, 3=Cold."""
    if activation == float("-inf"):
        return 3
    if activation >= _TIER_LIVE_MIN:
        return 0
    if activation >= _TIER_HOT_MIN:
        return 1
    if activation >= _TIER_WARM_MIN:
        return 2
    return 3


def compute_fact_activation(metadata: dict, *, now: float | None = None) -> float:
    """Thin adapter from a fact's stored metadata to reinforce._actr_activation."""
    from services.mem0.reinforce import _actr_activation

    now = now if now is not None else time.time()
    try:
        created = int(metadata.get("created_at_unix") or metadata.get("timestamp") or 0)
    except (TypeError, ValueError):
        created = 0
    try:
        mention = int(metadata.get("mention_count", 1))
    except (TypeError, ValueError):
        mention = 1
    try:
        recall = int(metadata.get("recall_count", 0))
    except (TypeError, ValueError):
        recall = 0
    reinforced_at = metadata.get("reinforced_at") or []
    return _actr_activation(created, reinforced_at, mention, recall, now=now)


def is_gist_exempt(metadata: dict) -> bool:
    """Facts that must never be silently compressed into a gist.

    - importance > GIST_MAX_IMPORTANCE: the extractor judged this
      load-bearing; compressing away its specifics would be a real loss.
    - attribute-tagged facts: these are the current value of a
      single-valued/temporal attribute (see attribute_registry.py) and back
      the deterministic conflict route in
      ingest._attribute_conflict_check — collapsing several attribute
      history rows into a vague gist would break that lookup.
    - already a gist (``gist_of`` present): never compress a compression.
    """
    if not isinstance(metadata, dict):
        return True
    try:
        imp = int(metadata.get("importance", 0))
    except (TypeError, ValueError):
        imp = 0
    if imp > GIST_MAX_IMPORTANCE:
        return True
    if (metadata.get("attribute") or "").strip():
        return True
    if metadata.get("gist_of"):
        return True
    return False


async def recompute_tiers_for_user(user_id: str, *, dry_run: bool = True) -> dict:
    """Recompute ``tier`` for every active fact of ``user_id`` from its
    current ACT-R activation.

    ``dry_run=True`` (default) computes and logs every transition but never
    writes. Returns ``{"checked": int, "changed": int, "transitions": {...}}``
    — in dry-run, ``changed`` counts transitions that *would* be written.
    """
    import asyncio
    from services.mem0 import client as client_mod
    from services.oauth_manager import ensure_valid_token

    mem = await asyncio.to_thread(client_mod._get_mem0)
    if not mem:
        return {"checked": 0, "changed": 0, "transitions": {}}

    rows = await client_mod.get_all_memories(user_id)
    now = time.time()
    checked = 0
    changed = 0
    transitions: dict[str, int] = {}
    if not dry_run:
        await ensure_valid_token()
        client_mod._refresh_mem0_client_token()
    for r in rows:
        md = r.get("metadata") or {}
        if not isinstance(md, dict) or md.get("valid_to"):
            continue  # archived rows are frozen — decay doesn't touch them
        old_tier = md.get("tier")
        try:
            old_tier = int(old_tier)
        except (TypeError, ValueError):
            old_tier = None
        activation = compute_fact_activation(md, now=now)
        new_tier = activation_to_tier(activation)
        checked += 1
        if old_tier == new_tier:
            continue
        key = f"{old_tier}->{new_tier}"
        transitions[key] = transitions.get(key, 0) + 1
        log.info(
            "[TIER_DECAY user=...%s id=%s activation=%.3f tier=%s->%d dry_run=%s]",
            str(user_id)[-4:],
            r.get("id"),
            activation,
            old_tier,
            new_tier,
            dry_run,
        )
        if dry_run:
            changed += 1
            continue
        try:
            from services.mem0.integrity import get_application_service

            service = get_application_service()
            await asyncio.to_thread(
                service.update_metadata,
                mem,
                r.get("id"),
                {"tier": new_tier},
                record=r,
                operation="tier_decay",
            )
            changed += 1
        except Exception as exc:
            log.warning(
                "tier decay update failed for %s (non-fatal): %s", r.get("id"), exc
            )
    return {"checked": checked, "changed": changed, "transitions": transitions}


async def gist_compress_for_user(
    user_id: str,
    *,
    dry_run: bool = True,
    max_clusters: int | None = None,
) -> dict:
    """Cluster Cold-tier, non-exempt facts by shared entity and collapse
    each cluster into fewer gist facts via ``dedup.synthesize_cluster``.

    Clustering key is the cluster member's most-mentioned linked entity
    (from the graph layer) — a fact with no linked entity is left alone
    rather than guessed into a cluster it may not belong to. Bounded to
    ``max_clusters`` (default ``GIST_MAX_CLUSTERS_PER_RUN``) LLM-consolidation
    calls per run so a large backlog spreads across several nightly runs
    instead of one expensive burst.
    """
    import asyncio
    from services.mem0 import client as client_mod
    from services.mem0 import graph
    from services.mem0.dedup import synthesize_cluster
    from services.oauth_manager import ensure_valid_token

    if max_clusters is None:
        max_clusters = GIST_MAX_CLUSTERS_PER_RUN

    mem = await asyncio.to_thread(client_mod._get_mem0)
    if not mem:
        return {"clusters": 0, "compressed": 0, "archived": 0}

    rows = await client_mod.get_all_memories(user_id)
    candidates = []
    for r in rows:
        md = r.get("metadata") or {}
        if not isinstance(md, dict) or md.get("valid_to"):
            continue
        try:
            tier = int(md.get("tier", 1))
        except (TypeError, ValueError):
            continue
        if tier != 3:
            continue
        if is_gist_exempt(md):
            continue
        candidates.append(r)

    clusters: dict[int, list[dict]] = {}
    for r in candidates:
        fid = r.get("id")
        if not fid:
            continue
        entities = await asyncio.to_thread(graph.get_fact_entities, fid)
        if not entities:
            continue  # no clustering key — left untouched, never guessed
        key_entity = max(entities, key=lambda e: e.mention_count)
        clusters.setdefault(key_entity.id, []).append(r)

    result = {"clusters": 0, "compressed": 0, "archived": 0}
    now_ts = int(time.time())
    if not dry_run:
        await ensure_valid_token()
        client_mod._refresh_mem0_client_token()
    for entity_id, members in list(clusters.items())[:max_clusters]:
        if len(members) < GIST_MIN_CLUSTER_SIZE:
            continue
        texts = [m.get("memory") or m.get("text") or "" for m in members]
        texts = [t for t in texts if t]
        if len(texts) < GIST_MIN_CLUSTER_SIZE:
            continue
        consolidated = await synthesize_cluster(texts)
        if not consolidated:
            continue  # fail-open: leave the cluster untouched
        result["clusters"] += 1
        log.info(
            "[GIST_COMPRESS user=...%s entity_id=%s members=%d->%d dry_run=%s] %r",
            str(user_id)[-4:],
            entity_id,
            len(members),
            len(consolidated),
            dry_run,
            consolidated,
        )
        if dry_run:
            result["compressed"] += len(consolidated)
            continue

        member_ids = [m.get("id") for m in members if m.get("id")]
        base_meta = {
            "category": (members[0].get("metadata") or {}).get("category", "meta"),
            "importance": max(
                1,
                min(
                    int((m.get("metadata") or {}).get("importance", 2)) for m in members
                ),
            ),
            "tier": 3,
            "gist_of": ",".join(member_ids),
            "timestamp": now_ts,
        }
        try:
            from services.mem0.integrity import get_application_service

            service = get_application_service()
            source_records = {
                str(member["id"]): member for member in members if member.get("id")
            }
            created = await asyncio.to_thread(
                service.consolidate,
                mem,
                user_id=str(user_id),
                source_ids=member_ids,
                outputs=[
                    {"text": gist_text, "metadata": dict(base_meta)}
                    for gist_text in consolidated
                ],
                reason="gist",
                source_records=source_records,
            )
            new_ids = [
                fact.get("legacy_id") or fact.get("fact_id")
                for fact in created
                if fact.get("legacy_id") or fact.get("fact_id")
            ]
            if not service.canonical_enabled:
                for new_id in new_ids:
                    await asyncio.to_thread(graph.add_edge, new_id, entity_id)
            result["compressed"] += len(new_ids)
            result["archived"] += len(member_ids)
        except Exception as exc:
            log.warning("gist consolidation failed (non-fatal): %s", exc)
    return result

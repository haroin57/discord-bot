"""Memory ingestion: P2 extraction-gate path + legacy Mem0-standard fallback."""

import asyncio
import logging

from services.mem0.client import _get_mem0, _refresh_mem0_client_token
from services.mem0.extract import extract_facts_for_self
from services.mem0.search import search_memories
from services.oauth_manager import ensure_valid_token

log = logging.getLogger(__name__)


async def add_memories(
    messages: list[dict] | None = None,
    user_id: str | None = None,
    metadata: dict | None = None,
    *,
    user_text: str | None = None,
    assistant_text: str | None = None,
    context_hint: str = "",
) -> dict | None:
    """Store conversation for fact extraction.

    Supports both legacy `(messages, user_id, metadata)` and new
    `(user_text=, assistant_text=, user_id=, context_hint=, metadata=)`.
    """
    # ── 1. Normalize signature into (user_text, assistant_text, context_hint) ──
    if messages is not None and user_text is None:
        for m in messages or []:
            r = m.get("role")
            c = m.get("content", "") if isinstance(m.get("content"), str) else ""
            if r == "user" and user_text is None:
                user_text = c
            elif r == "assistant" and assistant_text is None:
                assistant_text = c
            elif r == "system" and not context_hint:
                context_hint = c
    user_text = user_text or ""
    assistant_text = assistant_text or ""

    mem = _get_mem0()
    if not mem:
        return None

    # Late import so .env reload (via restart) is honored
    from config import (
        MEM0_GATE_ENABLED, MEM0_GATE_MIN_IMPORTANCE,
        MEM0_CLIENT_DEDUP_ENABLED, MEM0_CLIENT_DEDUP_THRESHOLD,
        MEM0_DEDUP_TWO_STAGE, MEM0_DEDUP_SEARCH_LIMIT, MEM0_DEDUP_DRY_RUN,
    )

    try:
        await ensure_valid_token()
        _refresh_mem0_client_token()
        import time as _time
        # Caller-supplied metadata is allowlisted before merging so it cannot
        # overwrite trust-relevant keys (gate_version, timestamp, importance,
        # category, tags). New caller-controlled keys must be added here
        # explicitly.
        _ALLOWED_CALLER_META = {"channel_id", "channel_name", "source"}
        base_meta = {
            "timestamp": int(_time.time()),
            "gate_version": "v2" if MEM0_GATE_ENABLED else "v1",
        }
        if metadata:
            for k, v in metadata.items():
                if k in _ALLOWED_CALLER_META and k not in base_meta:
                    base_meta[k] = v

        # ── 2. Gate path (P2): Haiku → filter → infer=False raw insert ──
        if MEM0_GATE_ENABLED:
            facts = await extract_facts_for_self(user_text, assistant_text, context_hint)

            # Cheap synchronous filter pass first — drops obvious non-self /
            # low-importance facts without firing any ChromaDB queries.
            candidates: list[dict] = []
            skip_low = skip_other = skip_dup = 0
            for f in facts:
                if f.get("speaker") != "self":
                    skip_other += 1
                    continue
                if int(f.get("importance", 0)) < MEM0_GATE_MIN_IMPORTANCE:
                    skip_low += 1
                    continue
                candidates.append(f)

            # Intra-batch dedup: two facts with identical text in the same
            # call would both pass the parallel dedup-check (neither is in
            # the store yet) and both get inserted. Drop within-batch
            # duplicates by `text` before firing any ChromaDB queries.
            seen_texts: set[str] = set()
            deduped: list[dict] = []
            for f in candidates:
                t = f.get("text", "")
                if t in seen_texts:
                    skip_dup += 1
                    continue
                seen_texts.add(t)
                deduped.append(f)
            candidates = deduped

            # Parallel near-dup check with optional 2-stage LLM judgment.
            kept: list[dict] = candidates
            if MEM0_CLIENT_DEDUP_ENABLED and candidates:
                search_limit = MEM0_DEDUP_SEARCH_LIMIT
                dedup_results = await asyncio.gather(
                    *(search_memories(f["text"], user_id, limit=search_limit) for f in candidates),
                    return_exceptions=True,
                )

                if MEM0_DEDUP_TWO_STAGE:
                    from services.mem0.dedup import check_near_dup
                    kept = []
                    # Run 2-stage checks in parallel
                    async def _check_one(f, near):
                        if isinstance(near, Exception):
                            return (f, False)
                        is_dup, reason = await check_near_dup(
                            f["text"], f.get("category"), near,
                            dry_run=MEM0_DEDUP_DRY_RUN,
                        )
                        return (f, is_dup)
                    check_results = await asyncio.gather(
                        *(_check_one(f, near) for f, near in zip(candidates, dedup_results)),
                    )
                    for f, is_dup in check_results:
                        if is_dup and not MEM0_DEDUP_DRY_RUN:
                            skip_dup += 1
                        else:
                            kept.append(f)
                else:
                    # Legacy single-threshold dedup
                    kept = []
                    for f, near in zip(candidates, dedup_results):
                        if isinstance(near, Exception):
                            kept.append(f)
                            continue
                        if any((r.get("score") or 0) >= MEM0_CLIENT_DEDUP_THRESHOLD for r in near):
                            skip_dup += 1
                        else:
                            kept.append(f)

            # Parallel raw insert: same rationale — three ChromaDB writes in
            # serial added ~3 x latency. asyncio.gather them and tolerate
            # individual failures via return_exceptions.
            async def _insert_one(f: dict):
                fact_meta = dict(base_meta)
                imp = int(f.get("importance", 3))
                # importance=2 → force tier=Warm so weekly compact ages it out faster
                tier = 2 if imp <= 2 else f.get("tier")
                fact_meta.update({
                    "category":    f.get("category"),
                    "subcategory": f.get("subcategory"),
                    "importance":  imp,
                    "tier":        tier,
                    "tags":        f.get("tags") or [],
                })
                try:
                    return await asyncio.to_thread(
                        mem.add,
                        [{"role": "user", "content": f["text"]}],
                        user_id=user_id, metadata=fact_meta, infer=False,
                    )
                except Exception as e:
                    log.warning("Mem0 raw insert failed: %s", e)
                    return None

            insert_results = await asyncio.gather(*(_insert_one(f) for f in kept)) if kept else []
            results = [r for r in insert_results if r is not None]

            log.info(
                "Mem0 gate: facts=%d kept=%d skip(low=%d,other=%d,dup=%d) user=...%s",
                len(facts), len(kept), skip_low, skip_other, skip_dup, str(user_id)[-4:],
            )
            return {"results": results, "kept": len(kept), "skipped": {
                "low_importance": skip_low, "other_speaker": skip_other, "near_dup": skip_dup,
            }}

        # ── 3. Legacy path: Mem0 standard fact extraction (fallback) ──
        # context_hint is INTENTIONALLY excluded from legacy_msgs even when
        # MEM0_LEGACY_CONTEXT is on. The Phase 1 root cause (other-speaker
        # contamination) was the system-message channel-history path; we keep
        # it sealed here regardless of which gate is enabled. Also filter the
        # caller-supplied `messages` to user/assistant roles only so a future
        # caller that passes a raw list with role=system can't re-introduce
        # the contamination vector.
        if messages:
            legacy_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]
        else:
            legacy_msgs = []
        if not legacy_msgs:
            if user_text:
                legacy_msgs.append({"role": "user", "content": user_text})
            if assistant_text:
                legacy_msgs.append({"role": "assistant", "content": assistant_text})
        # _classify_for_metadata() was previously fired here, adding a second
        # Haiku call on top of Mem0's own internal extractor. The gate path
        # (above) is the canonical source of taxonomy metadata; in legacy
        # fallback we accept the simpler Mem0 default and skip the redundant
        # classifier so this path costs 1 LLM call, not 2.
        result = await asyncio.to_thread(
            mem.add, legacy_msgs, user_id=user_id, metadata=base_meta
        )
        count = len(result.get("results", [])) if isinstance(result, dict) else 0
        if count:
            log.info("Mem0(legacy): extracted %d user=...%s", count, str(user_id)[-4:])
            if log.isEnabledFor(logging.DEBUG):
                previews = " | ".join(
                    str(r.get("memory", r.get("text", "")))[:60]
                    for r in (result.get("results") or [])[:5]
                )
                log.debug("Mem0(legacy) previews: %s", previews)
        return result
    except Exception as exc:
        log.warning("Mem0 add error: %s", exc)
        return None

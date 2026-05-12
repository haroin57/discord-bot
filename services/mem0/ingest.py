"""Memory ingestion: P2 extraction-gate path + legacy Mem0-standard fallback."""

import asyncio
import logging

from services.mem0.client import _get_mem0, _refresh_mem0_client_token
from services.mem0.extract import _classify_for_metadata, extract_facts_for_self
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
    )

    try:
        await ensure_valid_token()
        _refresh_mem0_client_token()
        import time as _time
        # Caller-supplied metadata is allowlisted before merging so it cannot
        # overwrite trust-relevant keys (gate_version, timestamp, importance,
        # category, tags). New caller-controlled keys must be added here
        # explicitly.
        _ALLOWED_CALLER_META = {"channel_id", "channel_name"}
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
            kept: list[dict] = []
            skip_low = skip_other = skip_dup = 0
            for f in facts:
                if f.get("speaker") != "self":
                    skip_other += 1
                    continue
                if int(f.get("importance", 0)) < MEM0_GATE_MIN_IMPORTANCE:
                    skip_low += 1
                    continue
                if MEM0_CLIENT_DEDUP_ENABLED:
                    near = await search_memories(f["text"], user_id, limit=3)
                    if any((r.get("score") or 0) >= MEM0_CLIENT_DEDUP_THRESHOLD for r in near):
                        skip_dup += 1
                        continue
                kept.append(f)

            results = []
            for f in kept:
                fact_meta = dict(base_meta)
                fact_meta.update({
                    "category":    f.get("category"),
                    "subcategory": f.get("subcategory"),
                    "importance":  f.get("importance"),
                    "tags":        f.get("tags") or [],
                })
                try:
                    res = await asyncio.to_thread(
                        mem.add,
                        [{"role": "user", "content": f["text"]}],
                        user_id=user_id, metadata=fact_meta, infer=False,
                    )
                    results.append(res)
                except Exception as e:
                    log.warning("Mem0 raw insert failed: %s", e)
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
        # it sealed here regardless of which gate is enabled.
        legacy_msgs = messages or []
        if not legacy_msgs:
            if user_text:
                legacy_msgs.append({"role": "user", "content": user_text})
            if assistant_text:
                legacy_msgs.append({"role": "assistant", "content": assistant_text})
        cls = await _classify_for_metadata(legacy_msgs)
        if cls:
            base_meta.update(cls)
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

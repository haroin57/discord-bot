"""Memory ingestion: P2 extraction-gate path + legacy Mem0-standard fallback.

``add_memories`` is the single entry point; it normalizes the call
signature, then dispatches to the gate path (LLM extraction → filter →
dedup → raw insert + side-effect indexing) or the legacy path (the memory
store's built-in extractor). Every stage lives in its own helper so the
pipeline reads top-to-bottom instead of as one interleaved function.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar, copy_context
from functools import partial
import fcntl
import hashlib
import logging
import time
import json
import os
import sqlite3
import uuid
from pathlib import Path

from .client import _get_mem0
from .extract import extract_full_signals, normalize_fact
from .attribute_registry import can_supersede, fact_metadata, same_fact_domain, validate_effective_interval
from .search import search_memories
from .settings import settings

log = logging.getLogger(__name__)

_INSERT_TASKS: dict[tuple, asyncio.Task] = {}
_INGEST_JOBS: set[asyncio.Task] = set()
_DOMAIN_WORKERS: ContextVar[list | None] = ContextVar("mem0_domain_workers", default=None)


async def _write_thread(function, *args, **kwargs):
    """Executor futures outlive asyncio.run's cancellation of all Tasks.

    The domain owner drains these futures before releasing the file locks.
    Never cancel the executor future merely because its waiter is cancelled.
    """
    future = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, partial(function, *args, **kwargs))
    workers = _DOMAIN_WORKERS.get()
    if workers is not None:
        workers.append(future)
    return await asyncio.shield(future)

# Caller-supplied metadata is allowlisted before merging so it cannot
# overwrite trust-relevant keys (gate_version, timestamp, importance,
# category, tags). New caller-controlled keys must be added here explicitly.
# `persona` / `episode_id` / `interest_tags` let the recall path carry an
# optional persona label and link a fact back to the episode that produced
# it. `interest_tags` must be a CSV string because ChromaDB rejects
# list-valued metadata; the caller is responsible for the CSV conversion.
_ALLOWED_CALLER_META = {
    "channel_id", "channel_name", "source",
    "persona", "episode_id", "interest_tags",
    # replay/schema-abstraction provenance: comma-joined instance fact ids
    # a consolidation-derived fact was generalized from (see replay.py).
    "derived_from",
    "request_id", "source_message_ids", "source_author_id", "source_timestamp",
    "batch_id", "batch_size", "event_time",
    "valid_from", "valid_to",
}

_EMPTY_SKIPPED = {"low_importance": 0, "other_speaker": 0, "near_dup": 0}


def _fact_key(user_id: str, text: str, metadata: dict | None = None) -> str:
    md = metadata or {}
    identity = [str(user_id), text.strip(), md.get("scope") or "global",
                md.get("attribute") or "", md.get("condition") or "",
                md.get("valid_from"), md.get("valid_to")]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


@asynccontextmanager
async def _ingestion_domains(user_id, candidates, base_meta):
    """Serialize decisions through commit for cooperating processes.

    Single-valued attributes share a lock even when the proposed text differs.
    Other facts lock by exact identity. Acquire stripes in one global order;
    nonblocking flock avoids filling the executor with blocked lock waiters.
    """
    from .attribute_registry import get_conflict_group
    namespace = [settings.CHROMA_MODE, settings.CHROMA_HOST, settings.CHROMA_PORT,
                 settings.MEM0_COLLECTION_NAME, str(user_id)]
    stripes = set()
    for candidate in candidates:
        md = fact_metadata(candidate, base_meta)
        attribute = md.get("attribute") or ""
        identity = (["attribute", sorted(get_conflict_group(attribute)),
                     md.get("scope") or "global", md.get("condition") or ""]
                    if attribute
                    else ["fact", _fact_key(user_id, candidate["text"], md)])
        digest = hashlib.sha256(json.dumps([namespace, identity], ensure_ascii=False).encode()).hexdigest()
        stripes.add(int(digest[:8], 16) % 256)
    directory = Path(settings.STATE_DIR) / "ingest_domain_locks"
    handles = []
    workers = []
    token = _DOMAIN_WORKERS.set(workers)
    def open_lock(stripe):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(directory / f"{stripe}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        for stripe in sorted(stripes):
            fd = open_lock(stripe)  # No unowned executor FD on cancellation.
            handles.append(fd)
            deadline = time.monotonic() + 120
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("memory ingest domain is busy; retry retained input")
                    await asyncio.sleep(0.02)
        yield
    finally:
        # Child tasks may be cancelled at shutdown while provider threads are
        # still committing. Keep the cross-process decision lock until those
        # writes really finish; retained source input repairs projections.
        for future in workers:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not future.cancelled():
                future.exception()
        _DOMAIN_WORKERS.reset(token)
        for fd in reversed(handles):
            os.close(fd)  # closing the owning descriptor releases its flock


async def _owned_ingest(awaitable):
    """Cancellation of a caller cannot release a domain before its write drains."""
    task = asyncio.create_task(awaitable)
    _INGEST_JOBS.add(task)
    def done(completed):
        _INGEST_JOBS.discard(completed)
        if not completed.cancelled():
            completed.exception()
    task.add_done_callback(done)
    return await asyncio.shield(task)




# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def add_memories(
    messages: list[dict] | None = None,
    user_id: str | None = None,
    metadata: dict | None = None,
    *,
    user_text: str | None = None,
    assistant_text: str | None = None,
    context_hint: str = "",
    pre_extracted_facts: list[dict] | None = None,
) -> dict | None:
    """Store conversation for fact extraction.

    Supports both legacy `(messages, user_id, metadata)` and new
    `(user_text=, assistant_text=, user_id=, context_hint=, metadata=)`.

    Facts about the user are extracted from ``user_text`` (with
    ``assistant_text`` / ``context_hint`` as supporting context).

    ``pre_extracted_facts`` — when supplied, skips the LLM extraction
    step and uses the provided facts as-is, so the caller can avoid a
    second extraction pass. Each fact must already be in the same shape
    ``extract_full_signals`` would have returned under its ``facts`` key.
    """
    source_spans = _message_source_spans(messages) if user_text is None else []
    user_text, assistant_text, context_hint = _normalize_signature(
        messages, user_text, assistant_text, context_hint,
    )

    try:
        if not user_id:
            raise ValueError("memory subject user_id is required")
        # Initialization failures must be retryable outcomes too.
        mem = await asyncio.to_thread(_get_mem0)
        if not mem:
            raise RuntimeError("memory store unavailable")
        # The auth backend refreshes access tokens internally, so no explicit
        # token refresh is needed here before touching the store.
        base_meta = _build_base_meta(metadata)
        if settings.MEM0_GATE_ENABLED:
            return await _add_via_gate(
                mem, user_id, user_text, assistant_text, context_hint,
                base_meta, pre_extracted_facts,
                source_spans=source_spans,
            )
        return await _add_legacy(
            mem, messages, user_text, assistant_text, user_id, base_meta,
        )
    except Exception as exc:
        # Surface the reason to callers (MCP server / /remember): the old
        # bare None made every failure look identical and pointed users at
        # "server logs" that a stdio MCP child never gets captured from
        # (2026-07-04 incident: three adds failed invisibly around a bot
        # restart and the WARNING below vanished into a dropped stderr).
        log.warning("Mem0 add error: %s", exc, exc_info=True)
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "status": "error", "retryable": True, "failed": 1,
            "results": [], "kept": 0,
            "skipped": dict(_EMPTY_SKIPPED),
        }


def _normalize_signature(
    messages: list[dict] | None,
    user_text: str | None,
    assistant_text: str | None,
    context_hint: str,
) -> tuple[str, str, str]:
    """Fold the legacy ``messages`` list into (user_text, assistant_text,
    context_hint); explicit keyword args win."""
    if messages is not None:
        user_parts, assistant_parts = [], []
        for m in messages:
            if not isinstance(m, dict):
                continue
            r, c = m.get("role"), m.get("content")
            if not isinstance(c, str):
                continue
            if r == "user":
                user_parts.append(c)
            elif r == "assistant":
                assistant_parts.append(c)
            elif r == "system" and not context_hint:
                context_hint = c
        if user_text is None:
            user_text = "\n\n".join(user_parts)
        if assistant_text is None:
            assistant_text = "\n\n".join(assistant_parts)
    return user_text or "", assistant_text or "", context_hint


def _message_source_spans(messages):
    """Keep explicit source provenance aligned with the normalized user text."""
    spans, offset = [], 0
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        spans.append({"start": offset, "end": offset + len(content),
                      **{k: message[k] for k in ("source_id", "author_id", "source_timestamp") if k in message}})
        offset += len(content) + 2
    return spans


def _assign_source_spans(signals, spans):
    for fact in signals.get("facts", []):
        offset = fact.get("source_offset")
        if not isinstance(offset, int):
            continue
        for span in spans or []:
            if span["start"] <= offset < span["end"] and offset + len(fact.get("source_quote") or "") <= span["end"]:
                source = span.get("source_id")
                author = span.get("author_id")
                if isinstance(source, str) and source and len(source) <= 512:
                    fact.update(source_message_ids=json.dumps([source]), request_id=source)
                if isinstance(author, str) and author and len(author) <= 512:
                    fact["source_author_id"] = author
                if "source_timestamp" in span:
                    value = span["source_timestamp"]
                    from .attribute_registry import observation_time
                    if isinstance(value, bool) or observation_time({"source_timestamp": value}) is None:
                        raise ValueError("invalid message source_timestamp")
                    fact["source_timestamp"] = value
                break


def _build_base_meta(metadata: dict | None) -> dict:
    """Timestamp + gate version + allowlisted caller metadata."""
    base_meta = {
        "timestamp": int(time.time()),
        "gate_version": "v2" if settings.MEM0_GATE_ENABLED else "v1",
    }
    if metadata:
        validate_effective_interval(metadata)
        for k, v in metadata.items():
            if k in _ALLOWED_CALLER_META and k not in base_meta:
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False, allow_nan=False)
                if isinstance(v, (str, int, float, bool)):
                    base_meta[k] = v
    if "source_timestamp" not in base_meta and "event_time" in base_meta:
        base_meta["source_timestamp"] = base_meta["event_time"]
    if "source_timestamp" in base_meta:
        from .attribute_registry import observation_time
        if isinstance(base_meta["source_timestamp"], bool) or observation_time(base_meta) is None:
            raise ValueError("source_timestamp must be finite Unix seconds or ISO datetime")
    return base_meta


# ---------------------------------------------------------------------------
# Gate path (P2): LLM extract → filter → dedup → infer=False raw insert
# ---------------------------------------------------------------------------
async def _add_via_gate(
    mem, user_id, user_text: str, assistant_text: str, context_hint: str,
    base_meta: dict, pre_extracted_facts: list[dict] | None,
    *, source_spans=None,
) -> dict:
    if pre_extracted_facts is None:
        from . import ingest_journal as journal
        budget = max(128, int(settings.MEM0_EXTRACT_INPUT_CHARS))
        chunks = max(1, min(64, int(settings.MEM0_EXTRACT_MAX_CHUNKS)))
        capacity = budget + (chunks - 1) * (budget - min(200, budget // 8))
        source = base_meta.get("request_id") or base_meta.get("batch_id")
        key = journal.job_key(str(user_id), str(source)) if source else None
        existing_job = bool(key and await asyncio.to_thread(journal.has_job, key))
        if len(user_text) > capacity or existing_job:
            if not source:
                return {"status": "error", "error": "request_id_required_for_long_input", "retryable": True,
                        "continuation_id": "continuation:" + uuid.uuid4().hex, "results": [], "kept": 0}
            return await _owned_ingest(_resume_extraction_job(
                mem, user_id, user_text, assistant_text, context_hint, base_meta,
                source_spans or [], key, str(source), capacity))
    signals = await _obtain_signals(user_id, user_text, assistant_text, context_hint,
                                    pre_extracted_facts)
    # Compatibility for internal callers mocking the former tuple contract.
    if isinstance(signals, tuple):
        signals = {"facts": signals[0], "relations": signals[1]}
    _assign_source_spans(signals, source_spans)
    return await _owned_ingest(_process_signals(mem, user_id, signals, base_meta))


async def _process_signals(mem, user_id, signals, base_meta):
    facts, relations = signals.get("facts", []), signals.get("relations", [])
    candidates, skip_low, skip_other, skip_dup = _filter_candidates(facts)
    async with _ingestion_domains(user_id, candidates, base_meta):
        return await _store_candidates(mem, user_id, candidates, base_meta, signals, relations,
                                       skip_low, skip_other, skip_dup)


async def _resume_extraction_job(mem, user_id, user_text, assistant_text, context_hint,
                                 base_meta, source_spans, key, source, capacity):
    from . import ingest_journal as journal
    identity = [user_text, assistant_text, context_hint, source_spans,
                {k: v for k, v in base_meta.items() if k not in {"timestamp", "gate_version"}}]
    input_hash = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    async with journal.job_lock(key):
        # Drain checkpoint writers before releasing the job lock at shutdown.
        async with _ingestion_domains(user_id, [], base_meta):
            job = await _write_thread(journal.read_job, key, input_hash)
            if job["complete"]:
                result = job["result"] or {"status": "ok", "results": []}
                for outcome in result.get("results", []):
                    for fid in _result_ids(outcome):
                        row = await asyncio.to_thread(mem.get, fid)
                        md = (row.get("metadata") or {}) if isinstance(row, dict) else {}
                        if not isinstance(row, dict) or str(row.get("user_id") or md.get("user_id")) != str(user_id):
                            return {"status": "error", "error": "completed_job_target_missing", "retryable": False,
                                    "results": [], "continuation_id": source}
                return {**result, "replayed": True, "continuation_id": source}
            start = job["cursor"]
            end = min(len(user_text), start + capacity)
            signals = job["signals"]
            if signals is None:
                signals = await _obtain_signals(user_id, user_text[start:end], assistant_text,
                                               context_hint, None)
                for fact in signals.get("facts", []):
                    if isinstance(fact.get("source_offset"), int):
                        fact["source_offset"] += start
                _assign_source_spans(signals, source_spans)
                signals["_job_window_end"] = end
                if not signals.get("error") and signals.get("status", "ok") == "ok":
                    await _write_thread(journal.cache_signals, key, signals)
            else:
                end = int(signals["_job_window_end"])
            result = await _process_signals(mem, user_id, signals, base_meta)
            if result.get("status") != "ok" or result.get("error"):
                return {**result, "continuation_id": source, "next_offset": start, "total_chars": len(user_text)}
            previous = job["result"] or {}
            combined = {**result, "results": [*previous.get("results", []), *result.get("results", [])]}
            for counter in ("kept", "stored", "failed", "archived"):
                combined[counter] = previous.get(counter, 0) + result.get(counter, 0)
            done = end >= len(user_text)
            # Retain boundary context, while source receipts prevent overlap
            # from becoming an independent confirmation of the same event.
            overlap = min(200, max(128, int(settings.MEM0_EXTRACT_INPUT_CHARS)) // 8)
            cursor = end if done else max(start + 1, end - overlap)
            await _write_thread(journal.advance_job, key, cursor, combined, complete=done)
            if done:
                return {**combined, "continuation_id": source, "next_offset": cursor, "total_chars": len(user_text)}
            return {**combined, "status": "partial", "error": "continuation_required", "retryable": True,
                    "continuation_id": source, "next_offset": cursor, "total_chars": len(user_text)}


async def _store_candidates(mem, user_id, candidates, base_meta, signals, relations,
                            skip_low, skip_other, skip_dup):
    kept, dropped, reinforced_ids, _ = await _dedup_candidates(mem, user_id, candidates, base_meta)
    skip_dup += dropped
    reinforce_outcomes = {}
    confirmation_meta = {mid: {} for mid in set(reinforced_ids)}
    for f in candidates:
        if f.get("_reinforced_id") in confirmation_meta:
            confirmation_meta[f["_reinforced_id"]].update(_confirmation_metadata(f, base_meta))
    for events in confirmation_meta.values():
        if not events:  # Compatibility with legacy/mocked dedup contracts.
            events.update(_confirmation_metadata({}, base_meta))
    if reinforced_ids:
        from .reinforce import reinforce_existing_sync
        tasks = [(mid, event, metadata) for mid, events in confirmation_meta.items()
                 for event, metadata in events.items()]
        bumps = await asyncio.gather(*(_write_thread(
            reinforce_existing_sync, mem, mid, event_id=event,
            source_timestamp=metadata.get("source_timestamp"))
            for mid, event, metadata in tasks), return_exceptions=True)
        reinforce_outcomes = dict(zip(((mid, event) for mid, event, _ in tasks), bumps))
    repair_errors = []
    duplicate_support = []
    for mid in set(reinforced_ids):
        try:
            existing = await asyncio.to_thread(mem.get, mid)
            md = (existing.get("metadata") or {}) if isinstance(existing, dict) else {}
            if not isinstance(existing, dict):
                repair_errors.append("duplicate_target_missing")
                continue
            owner = existing.get("user_id") or md.get("user_id")
            if owner is not None and str(owner) != str(user_id):
                repair_errors.append("duplicate_target_owner_mismatch")
                continue
            known_events = json.loads(md.get("reinforced_event_ids") or "[]")
            for event in confirmation_meta[mid]:
                already_counted = event and (event in known_events or event in (md.get("request_id"), md.get("batch_id")))
                if event and not already_counted:
                    from .ingest_journal import event_recorded
                    already_counted = await asyncio.to_thread(event_recorded, mid, event)
                if reinforce_outcomes.get((mid, event)) is not True and not already_counted:
                    repair_errors.append("reinforcement_failed")
            if isinstance(existing, dict) and not md.get("valid_to"):
                repaired_fact = {**md, "text": existing.get("memory") or existing.get("text") or "",
                                 "entities": json.loads(md.get("entities_json") or "[]")}
                projection_errors = await _index_side_stores(user_id, repaired_fact, md, md, [mid])
                if isinstance(projection_errors, list) and projection_errors:
                    repair_errors.append("projection_incomplete")
                duplicate_support.extend((f, mid) for f in candidates
                                         if f["text"] == repaired_fact["text"]
                                         and same_fact_domain(fact_metadata(f, base_meta), fact_metadata(existing)))
            pending = json.loads(md.get("pending_predecessors") or "[]")
            if pending:
                repaired = await _archive_outdated_facts(
                    mem, [tuple(p) for p in pending], user_id=user_id,
                    new_fact_id=mid, candidate_metadata=md)
                if repaired != len(pending):
                    repair_errors.append("archive_incomplete")
            successor = json.loads(md.get("pending_successor") or "null")
            if successor and not await _archive_historical_observation(mem, mid, successor, user_id):
                repair_errors.append("historical_archive_incomplete")
        except Exception:
            repair_errors.append("duplicate_repair_failed")
    if settings.MEM0_ATTRIBUTE_CONFLICT_ENABLED:
        for f in kept:
            predecessors = f.setdefault("_predecessors", [])
            predecessors.extend(await _attribute_conflict_check(
                mem, user_id, [f], exclude_ids={eid for eid, _ in predecessors},
                base_meta=base_meta))
    groups = {}
    for index, f in enumerate(kept):
        md = fact_metadata(f, base_meta)
        attr = md.get("attribute")
        key = (attr, md.get("scope"), md.get("condition") or "", md.get("valid_from"), md.get("valid_to")) if attr else ("independent", index)
        groups.setdefault(key, []).append(f)
    semaphore = asyncio.Semaphore(4)

    async def insert_group(items):
        outcomes = []
        committed = []
        async with semaphore:
            for f in sorted(items, key=lambda row: row.get("source_offset", 0)):
                f = dict(f)
                predecessors = list(f.get("_predecessors") or [])
                classification_unknown = False
                if settings.MEM0_CONFLICT_DETECTION_ENABLED:
                    from .conflict import classify_conflict
                    for previous, previous_id in committed:
                        old_md, new_md = fact_metadata(previous, base_meta), fact_metadata(f, base_meta)
                        if can_supersede(old_md, new_md):
                            decision = await classify_conflict(previous["text"], f["text"],
                                existing_metadata=old_md, candidate_metadata=new_md)
                            if decision.get("retryable"):
                                classification_unknown = True
                                break
                            kind = decision.get("conflict_type")
                            if kind in ("update", "correction"):
                                predecessors.append((previous_id, kind))
                if classification_unknown:
                    outcomes.append((f, {"error": "conflict_classification_unavailable", "results": []}, 0))
                    continue
                f["_predecessors"] = list(dict.fromkeys(predecessors))
                outcome = await _insert_one(mem, user_id, f, base_meta)
                ids = _result_ids(outcome)
                count = 0
                if ids:
                    committed.append((f, ids[0]))
                    if predecessors:
                        count = await _archive_outdated_facts(
                            mem, f["_predecessors"], user_id=user_id, new_fact_id=ids[0],
                            candidate_metadata=fact_metadata(f, base_meta))
                    if f.get("_historical_successor"):
                        retired = await _archive_historical_observation(mem, ids[0], f["_historical_successor"], user_id)
                        if retired:
                            count += 1
                        else:
                            outcome = {**outcome, "projection_errors": [*(outcome.get("projection_errors") or []),
                                                                         "historical_archive_incomplete"]}
                outcomes.append((f, outcome, count))
        return outcomes

    grouped = await asyncio.gather(*(insert_group(items) for items in groups.values()))
    results, committed_facts = [], []
    failed = archived = 0
    errors = ([signals["error"]] if signals.get("error") else []) + repair_errors
    for f, outcome, count in (entry for group in grouped for entry in group):
        if not _result_ids(outcome):
            failed += 1
            errors.append(str(outcome.get("error") or "insert_failed_or_unconfirmed") if isinstance(outcome, dict) else "insert_failed_or_unconfirmed")
            continue
        results.append(outcome)
        committed_facts.append((f, _result_ids(outcome)[0]))
        archived += count
        if count < len(f.get("_predecessors") or []):
            errors.append("archive_incomplete")
        if isinstance(outcome, dict) and outcome.get("projection_errors"):
            errors.append("projection_incomplete")
    # Only grounded relations supported by accepted observations are eligible.
    # The current relation table has no scope column. Do not promote a
    # conversation-scoped fact or a conditional observation to a global edge.
    quotes = {}
    if settings.MEM0_RELATION_EXTRACT_ENABLED:
        for f, fid in committed_facts + duplicate_support:
            if not f.get("source_quote"):
                continue
            source_md = fact_metadata(f, base_meta)
            if source_md.get("scope") != "global" or source_md.get("condition"):
                continue
            try:
                primary = await asyncio.to_thread(mem.get, fid)
                md = fact_metadata(primary) if isinstance(primary, dict) else {}
                if md.get("scope") == "global" and not md.get("condition"):
                    quotes[f["source_quote"]] = (fid, md)
            except Exception:
                errors.append("relation_source_unavailable")
    accepted_relations = [dict(r, source_fact_id=quotes[r["source_quote"]][0],
                               valid_from=quotes[r["source_quote"]][1].get("valid_from"),
                               valid_to=quotes[r["source_quote"]][1].get("valid_to"))
                          for r in relations if r.get("source_quote") in quotes]
    if settings.MEM0_RELATION_EXTRACT_ENABLED and accepted_relations:
        added = await _ingest_relations(user_id, accepted_relations)
        if added != len(accepted_relations):
            errors.append("relation_projection_incomplete")
    result = {"results": results, "kept": len(kept), "stored": len(results),
              "failed": failed, "archived": archived, "status": "ok",
              "skipped": {"low_importance": skip_low, "other_speaker": skip_other, "near_dup": skip_dup}}
    if errors:
        result.update(status="partial" if results else "error",
                      error=";".join(sorted(set(errors))), retryable=True)
    return result

async def _obtain_signals(
    user_id, user_text: str, assistant_text: str, context_hint: str,
    pre_extracted_facts: list[dict] | None,
) -> dict:
    if pre_extracted_facts is not None:
        facts = [f for raw in pre_extracted_facts if (f := normalize_fact(raw, programmatic=True)) is not None]
        result = {"facts": facts, "relations": [], "status": "ok"}
        if len(facts) != len(pre_extracted_facts):
            result.update(status="partial" if facts else "error",
                          error="invalid_pre_extracted_facts", retryable=True)
        return result
    return await extract_full_signals(user_text, assistant_text, context_hint,
                                      user_id=str(user_id) if user_id else None)

def _filter_candidates(facts: list[dict]) -> tuple[list[dict], int, int, int]:
    """Cheap synchronous filter pass — drops obvious non-self /
    low-importance facts and intra-batch text duplicates without firing any
    ChromaDB queries. Returns (candidates, skip_low, skip_other, skip_dup).
    """
    min_importance = settings.MEM0_GATE_MIN_IMPORTANCE
    candidates: list[dict] = []
    skip_low = skip_other = skip_dup = 0
    for f in facts:
        if not isinstance(f, dict):
            skip_other += 1
            continue
        if f.get("speaker") != "self":
            skip_other += 1
            continue
        f = normalize_fact(f, programmatic=True)
        if f is None:
            skip_other += 1
            continue
        if f["importance"] < min_importance:
            skip_low += 1
            continue
        candidates.append(f)

    # Intra-batch dedup: two facts with identical text in the same call
    # would both pass the parallel dedup-check (neither is in the store
    # yet) and both get inserted. Drop within-batch duplicates by `text`
    # before firing any ChromaDB queries.
    seen_texts: dict[str, dict] = {}
    deduped: list[dict] = []
    for f in candidates:
        t = _fact_key("", f["text"], fact_metadata(f))
        if t in seen_texts:
            # Same content is stored once, but distinct source observations
            # retain separate durable confirmation identities.
            seen_texts[t].setdefault("_confirmation_sources", []).append(
                {k: f[k] for k in ("request_id", "source_timestamp") if k in f})
            skip_dup += 1
            continue
        seen_texts[t] = f
        deduped.append(f)
    return deduped, skip_low, skip_other, skip_dup


def _confirmation_metadata(f: dict, base_meta: dict) -> dict:
    """Unique source events for one fact; missing source IDs retain legacy behavior."""
    events = {}
    for source in [f, *(f.get("_confirmation_sources") or [])]:
        metadata = {**base_meta, **{k: source[k] for k in ("request_id", "source_timestamp") if k in source}}
        event = metadata.get("request_id") or metadata.get("batch_id")
        events.setdefault(event, metadata)
    return events


async def _dedup_candidates(
    mem, user_id, candidates: list[dict], base_meta: dict,
) -> tuple[list[dict], int, list[str], list[tuple[str, str]]]:
    """Near-duplicate suppression against the existing store.

    Returns ``(kept, dropped, reinforced_ids, conflict_archive)``:
      kept             — candidates to insert (a "merge" candidate has its
                         text replaced by the LLM-synthesized fact)
      dropped          — count of candidates dropped as duplicates
      reinforced_ids   — existing fact ids that absorbed a duplicate
      conflict_archive — (existing_id, conflict_type) pairs to soft-archive
                         after the new facts are inserted
    """
    if not settings.MEM0_CLIENT_DEDUP_ENABLED or not candidates:
        return list(candidates), 0, [], []

    # Phase R: scope フィルタ有効時に conversation fact が dedup 候補から
    # 消えると同一会話内の兄弟と dedup できなくなるので、この ingest の
    # 会話 channel_id を渡す。
    dedup_ch = base_meta.get("channel_id")
    dedup_ch = str(dedup_ch) if dedup_ch else None
    near_results = await asyncio.gather(
        *(
            search_memories(
                f["text"], user_id, limit=settings.MEM0_DEDUP_SEARCH_LIMIT,
                current_channel_id=dedup_ch,
            )
            for f in candidates
        ),
        return_exceptions=True,
    )

    if settings.MEM0_DEDUP_TWO_STAGE:
        return await _dedup_two_stage(candidates, near_results, base_meta=base_meta)
    return _dedup_single_threshold(candidates, near_results)


async def _dedup_two_stage(
    candidates: list[dict], near_results: list,
    *, base_meta: dict | None = None,
) -> tuple[list[dict], int, list[str], list[tuple[str, str]]]:
    """Cosine gate + LLM judgment, with merge (止揚) and conflict routes."""
    from .dedup import check_near_dup

    dry_run = settings.MEM0_DEDUP_DRY_RUN

    async def _check_one(f: dict, near):
        if isinstance(near, Exception):
            return (f, False, None, None, None)
        is_dup, _reason, existing_id, conflict_type, merged_text = await check_near_dup(
            f["text"], f.get("category"), near,
            dry_run=dry_run,
            detect_conflict=settings.MEM0_CONFLICT_DETECTION_ENABLED,
            allow_merge=settings.MEM0_DEDUP_SUPERSET_MERGE,
            candidate_metadata=fact_metadata(f, base_meta),
        )
        return (f, is_dup, existing_id, conflict_type, merged_text)

    check_results = await asyncio.gather(
        *(_check_one(f, near) for f, near in zip(candidates, near_results)),
    )

    kept: list[dict] = []
    dropped = 0
    reinforced_ids: list[str] = []
    # Phase B-2 / 止揚: facts that update / correct / merge with an existing
    # fact get their predecessor soft-archived *after* the new (or
    # synthesized) row is inserted, so we record the pairs here.
    conflict_archive: list[tuple[str, str]] = []
    for f, is_dup, existing_id, conflict_type, merged_text in check_results:
        if is_dup and not dry_run:
            dropped += 1
            if existing_id:
                f["_reinforced_id"] = existing_id
                reinforced_ids.append(existing_id)
            continue
        # 止揚: store the synthesized fact in place of the raw candidate so
        # the merged detail is what lands in ChromaDB; the older row is
        # archived below.
        if conflict_type == "merge" and merged_text and not dry_run:
            f = {**f, "text": merged_text}
            # Merge lineage belongs to the replacement, not just the archive.
            f["derived_from"] = str(existing_id)
        kept.append(f)
        if conflict_type in ("historical_update", "historical_correction") and existing_id and not dry_run:
            f["_historical_successor"] = [existing_id, conflict_type.removeprefix("historical_")]
        if (
            conflict_type in ("update", "correction", "merge")
            and existing_id
            and not dry_run
        ):
            conflict_archive.append((existing_id, conflict_type))
            f.setdefault("_predecessors", []).append((existing_id, conflict_type))
    return kept, dropped, reinforced_ids, conflict_archive


def _dedup_single_threshold(
    candidates: list[dict], near_results: list,
) -> tuple[list[dict], int, list[str], list[tuple[str, str]]]:
    """Legacy single-threshold dedup. NOTE: score is *distance* (small =
    similar), so we drop when score is BELOW the threshold, not above. The
    settings default of 0.85 is way too loose under this corrected
    semantic; set MEM0_CLIENT_DEDUP_THRESHOLD=0.20 if you actually rely on
    this path."""
    threshold = settings.MEM0_CLIENT_DEDUP_THRESHOLD
    kept: list[dict] = []
    dropped = 0
    for f, near in zip(candidates, near_results):
        if isinstance(near, Exception):
            kept.append(f)
            continue
        if any(
            (r.get("score") if r.get("score") is not None else 99) <= threshold
            and (r.get("memory") or r.get("text") or "").strip() == f["text"].strip()
            and same_fact_domain(fact_metadata(r), fact_metadata(f))
            for r in near
        ):
            dropped += 1
        else:
            kept.append(f)
    return kept, dropped, [], []


def _build_fact_meta(f: dict, base_meta: dict) -> dict:
    """Assemble the ChromaDB metadata dict for one extracted fact.

    ChromaDB metadata values must be scalar (str/int/float/bool); a list or
    None raises "Cannot convert Python object to MetadataValue" and aborts
    the whole ``mem.add`` — every fact in that turn silently dropped. So:
    tags become CSV, typed values become JSON strings, and None values are
    stripped at the end.
    """
    fact_meta = dict(base_meta)
    for key in ("source_quote", "source_role", "source_offset", "derived_from", "source_timestamp", "valid_from", "valid_to",
                "source_author_id", "source_message_ids", "request_id", "operation", "member_key", "retraction_target"):
        value = f.get(key)
        if isinstance(value, (str, int, float)):
            fact_meta[key] = value
    validate_effective_interval(fact_meta)
    if f.get("_predecessors"):
        fact_meta["pending_predecessors"] = json.dumps(f["_predecessors"])
    if f.get("_historical_successor"):
        fact_meta["pending_successor"] = json.dumps(f["_historical_successor"])
    if f.get("entities"):
        fact_meta["entities_json"] = json.dumps(f["entities"], ensure_ascii=False)
    imp = int(f.get("importance", 3))
    # importance=2 → force tier=Warm so weekly compact ages it out faster.
    # Otherwise default to tier=1 (Hot); the extract prompt doesn't emit
    # `tier`, so f.get("tier") is normally None. Explicit None check so a
    # pre_extracted_facts caller passing tier=0 (Live) isn't demoted to Hot.
    if imp <= 2:
        tier = 2
    else:
        raw_tier = f.get("tier")
        tier = 1 if raw_tier is None else int(raw_tier)
    raw_tags = f.get("tags") or []
    tags_csv = ",".join(
        str(t).replace(",", " ") for t in raw_tags if str(t).strip()
    )
    fact_meta.update({
        "category":    str(f.get("category") or "meta"),
        "subcategory": str(f.get("subcategory") or ""),
        "importance":  imp,
        "tier":        tier,
        "tags":        tags_csv,
    })
    # Phase A: attribute(統制語彙)を付与。決定的 conflict ルートのキー。
    # 空文字列は付けない(ChromaDB metadata を無駄に増やさない)。
    attr = f.get("attribute") or ""
    if settings.MEM0_ATTRIBUTE_ENABLED and attr:
        fact_meta["attribute"] = attr
    # v5 T7: typed value（quantity）。ChromaDB は dict を持てないので JSON
    # 文字列で保存。読む側は json.loads。
    val = f.get("value")
    if settings.MEM0_TYPED_VALUE_ENABLED and isinstance(val, dict):
        fact_meta["value_json"] = json.dumps(val, ensure_ascii=False, allow_nan=False)
    # Phase B: condition(time:/context:)を付与。
    cond = f.get("condition") or ""
    if settings.MEM0_CONDITION_ENABLED and cond:
        fact_meta["condition"] = cond
    # Phase C: scope。global は既定なので保存しない(metadata を節約)。
    # conversation scope には channel_id を紐付け、どの会話限定かを読み出し
    # 時に照合できるようにする。
    scope = f.get("scope") or ""
    if settings.MEM0_SCOPE_ENABLED and scope and scope != "global":
        if scope == "conversation":
            ch = base_meta.get("channel_id")
            fact_meta["scope"] = f"conversation:{ch}" if ch else "conversation"
        else:
            fact_meta["scope"] = scope
    # Final guard: drop any None values base_meta might still contain
    # (e.g. channel_id when called from a script).
    return {k: v for k, v in fact_meta.items() if v is not None}


def _result_ids(result) -> list[str]:
    if not isinstance(result, dict) or result.get("error"):
        return []
    return [str(r["id"]) for r in result.get("results", [])
            if isinstance(r, dict) and r.get("id")]


def _insert_raw_sync(mem, user_id, f: dict, fact_meta: dict):
    """Durable receipts and a shared lock protect redelivery across restarts.

    A process crash between the provider commit and receipt commit remains an
    uncertain outcome; ingest_key on the primary row allows reconciliation.
    """
    from .reinforce import memory_mutation_lock
    from .client import _ingest_receipt_lock
    identity = _fact_key(user_id, f["text"], fact_metadata(f, fact_meta))
    source_id = fact_meta.get("request_id") or fact_meta.get("batch_id") or ""
    store_identity = [settings.CHROMA_MODE, settings.CHROMA_HOST,
                      settings.CHROMA_PORT, settings.MEM0_COLLECTION_NAME]
    key = hashlib.sha256(json.dumps([store_identity, identity, source_id]).encode()).hexdigest()
    canonical_key = hashlib.sha256(json.dumps([store_identity, identity]).encode()).hexdigest()
    if not settings.MEM0_INGEST_HASH_DEDUP:
        return mem.add([{"role": "user", "content": f["text"]}],
                       user_id=user_id, metadata=fact_meta, infer=False)
    directory = Path(settings.STATE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    with memory_mutation_lock("ingest:" + identity), _ingest_receipt_lock(key):
        with closing(sqlite3.connect(directory / "mem0_ingest_receipts.sqlite3", timeout=30)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, response TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS canonical_receipts (key TEXT PRIMARY KEY, response TEXT NOT NULL)")
            row = db.execute("SELECT response FROM receipts WHERE key=?", (key,)).fetchone()
            if row:
                saved = json.loads(row[0])
                if saved.get("deleted"):
                    return {"error": "receipt_target_deleted", "results": []}
                if saved.get("pending"):
                    # The provider may have committed before an exception or
                    # process exit. Reconcile against the primary event key
                    # before issuing another write, not an embedding search.
                    from .client import mem_get_all
                    recovered = mem_get_all(mem, user_id=user_id, limit=None)
                    rows = recovered.get("results", []) if isinstance(recovered, dict) else recovered
                    matching = [r for r in (rows or []) if isinstance(r, dict)
                                and (r.get("metadata") or {}).get("ingest_key") == key and r.get("id")
                                and str(r.get("user_id") or (r.get("metadata") or {}).get("user_id")) == str(user_id)]
                    if matching:
                        result = {"results": [{"id": r["id"]} for r in matching]}
                        db.execute("INSERT OR REPLACE INTO receipts VALUES (?, ?)", (key, json.dumps(result)))
                        db.execute("INSERT OR REPLACE INTO canonical_receipts VALUES (?, ?)",
                                   (canonical_key, json.dumps({**result, "_receipt_ingest_key": key})))
                        return result
                # Stable source IDs identify replay of the same observation.
                else:
                    for fid in _result_ids(saved):
                        existing = mem.get(fid)
                        if not isinstance(existing, dict):
                            return {"error": "receipt_target_missing", "results": []}
                        md = existing.get("metadata") or {}
                        owner = existing.get("user_id") or md.get("user_id")
                        if str(owner) != str(user_id) or md.get("ingest_key") != saved.get("_receipt_ingest_key", key):
                            return {"error": "receipt_target_mismatch", "results": []}
                    if source_id or not (existing.get("metadata") or {}).get("valid_to"):
                        return saved
                db.execute("DELETE FROM receipts WHERE key=?", (key,))
            # Independent source events may arrive before the vector index
            # sees the prior write. Exact identity is checked against a durable
            # primary-ID receipt, not eventual-consistency search results.
            canonical = db.execute("SELECT response FROM canonical_receipts WHERE key=?", (canonical_key,)).fetchone()
            if canonical:
                saved = json.loads(canonical[0])
                ids = _result_ids(saved)
                existing = mem.get(ids[0]) if ids else None
                md = (existing.get("metadata") or {}) if isinstance(existing, dict) else {}
                owner = (existing.get("user_id") or md.get("user_id")) if isinstance(existing, dict) else None
                if (str(owner) == str(user_id) and not md.get("valid_to") and not md.get("archived")
                        and md.get("ingest_key") == saved.get("_receipt_ingest_key")):
                    result = {**saved, "_receipt_reused": True}
                    db.execute("INSERT OR REPLACE INTO receipts VALUES (?, ?)", (key, json.dumps(result)))
                    return result
            db.execute("INSERT OR REPLACE INTO receipts VALUES (?, ?)", (key, '{"pending": true}'))
            db.commit()
            result = mem.add([{"role": "user", "content": f["text"]}], user_id=user_id,
                             metadata={**fact_meta, "ingest_key": key}, infer=False)
            if _result_ids(result):
                db.execute("INSERT OR REPLACE INTO receipts VALUES (?, ?)",
                           (key, json.dumps(result, ensure_ascii=False, default=str)))
                db.execute("INSERT OR REPLACE INTO canonical_receipts VALUES (?, ?)",
                           (canonical_key, json.dumps({**result, "_receipt_ingest_key": key}, ensure_ascii=False, default=str)))
            return result


async def _insert_one(mem, user_id, f: dict, base_meta: dict):
    """One owned task drains the real write even if a waiter is cancelled."""
    try:
        fact_meta = _build_fact_meta(f, base_meta)
    except Exception as exc:
        return {"error": f"invalid_metadata:{type(exc).__name__}", "results": []}
    key = (asyncio.get_running_loop(), id(mem), str(Path(settings.STATE_DIR)),
           settings.CHROMA_MODE, settings.CHROMA_HOST, settings.CHROMA_PORT, settings.MEM0_COLLECTION_NAME,
           _fact_key(user_id, f["text"], fact_metadata(f, fact_meta)),
           fact_meta.get("request_id") or fact_meta.get("batch_id"))
    task = _INSERT_TASKS.get(key)
    if task is None:
        async def insert_and_project():
            try:
                result = await _write_thread(_insert_raw_sync, mem, user_id, f, fact_meta)
                ids = _result_ids(result)
                if not ids:
                    if isinstance(result, dict) and result.get("error"):
                        return result
                    return {"error": "insert_unconfirmed", "results": []}
                projection_fact, projection_meta = f, fact_meta
                if result.get("_receipt_reused"):
                    event = fact_meta.get("request_id") or fact_meta.get("batch_id")
                    from .reinforce import reinforce_existing_sync
                    bumped = await _write_thread(reinforce_existing_sync, mem, ids[0], event_id=event,
                                                 source_timestamp=fact_meta.get("source_timestamp"))
                    existing = await asyncio.to_thread(mem.get, ids[0])
                    projection_meta = existing.get("metadata") or {}
                    known_events = json.loads(projection_meta.get("reinforced_event_ids") or "[]")
                    from .ingest_journal import event_recorded
                    recorded = event and await asyncio.to_thread(event_recorded, ids[0], event)
                    if not bumped and not (recorded or (event and (event in known_events or event in (projection_meta.get("request_id"), projection_meta.get("batch_id"))))):
                        return {**result, "projection_errors": ["reinforcement_failed"]}
                    projection_fact = {**projection_meta, "text": existing.get("memory") or existing.get("text") or f["text"],
                                       "entities": json.loads(projection_meta.get("entities_json") or "[]")}
                if f.get("_confirmation_sources"):
                    from .reinforce import reinforce_existing_sync
                    from .ingest_journal import event_recorded
                    for event, source_meta in _confirmation_metadata(f, fact_meta).items():
                        if not event:
                            continue  # No stable source evidence, not a new confirmation.
                        bumped = await _write_thread(reinforce_existing_sync, mem, ids[0], event_id=event,
                                                     source_timestamp=source_meta.get("source_timestamp"))
                        if not bumped and not await asyncio.to_thread(event_recorded, ids[0], event):
                            return {**result, "projection_errors": ["reinforcement_failed"]}
                projection_errors = await _index_side_stores(user_id, projection_fact, projection_meta, base_meta, ids)
                from .search import invalidate_core_cache
                await _write_thread(invalidate_core_cache, str(user_id))
                if isinstance(projection_errors, list) and projection_errors:
                    result = {**result, "projection_errors": projection_errors}
                return result
            except Exception as exc:
                log.warning("Mem0 raw insert failed: %s", exc)
                return {"error": f"insert_failed:{type(exc).__name__}", "results": []}
        task = asyncio.create_task(insert_and_project())
        _INSERT_TASKS[key] = task
        def cleanup(done):
            if _INSERT_TASKS.get(key) is done:
                _INSERT_TASKS.pop(key, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(cleanup)
    return await asyncio.shield(task)

async def _index_side_stores(
    user_id, f: dict, fact_meta: dict, base_meta: dict, fact_ids: list[str],
) -> list[str]:
    """Mirror a freshly inserted fact into graph / BM25 / fact_store."""
    errors = []
    from . import graph
    try:
        fact_ids = [fid for fid in fact_ids if not await _write_thread(graph.is_source_retired, fid)]
        if not fact_ids:
            return []  # A terminal source retirement is not a retryable outage.
    except Exception:
        return ["source_retirement_unavailable"]
    # Graph layer: link extracted entities to the new fact.
    try:
        entities = f.get("entities") or []
        if entities:
            from . import graph
            linked_eids: list[int] = []
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                name = ent.get("name")
                etype = ent.get("type")
                if not name or not etype:
                    continue
                for fid in fact_ids:
                    eid = await _write_thread(
                        graph.upsert_entity, name, etype, str(user_id),
                        aliases=ent.get("aliases") or [], source_fact_id=fid,
                    )
                    if eid:
                        if eid not in linked_eids:
                            linked_eids.append(eid)
                        if not await _write_thread(graph.add_edge, fid, eid):
                            errors.append("graph_edge")
                    else:
                        errors.append("graph_entity")
            # Phase ④: entities just mentioned prime this session's next
            # search (see priming.py) — ingest and search share the same
            # buffer so either side of a turn can prime the other.
            if linked_eids:
                from . import priming
                priming.touch(
                    str(user_id), base_meta.get("channel_id"), linked_eids,
                )
    except Exception as e:
        log.warning("graph update failed (non-fatal): %s", e)
        errors.append("graph")

    # Phase A-1: index the new fact in the BM25 FTS5 store so hybrid
    # retrieval can rescue exact-term matches that cosine missed.
    try:
        from . import bm25_index
        for fid in fact_ids:
            indexed = await _write_thread(
                bm25_index.index_fact, fid, f["text"], str(user_id),
            )
            if not indexed:
                errors.append("bm25")
    except Exception as e:
        log.warning("bm25 index update failed (non-fatal): %s", e)
        errors.append("bm25")

    # v5 T8a: structured fact layer への導出インデックス登録（attribute 付き
    # fact のみ）。正本は Chroma、こちらは best-effort。
    try:
        attr = f.get("attribute") or ""
        if settings.MEM0_FACT_STORE_ENABLED and attr:
            from . import fact_store
            for fid in fact_ids:
                indexed = await _write_thread(
                    fact_store.record_fact,
                    fid, str(user_id), attr,
                    value_text=f.get("text", ""),
                    value_json=fact_meta.get("value_json"),
                    condition=f.get("condition") or "",
                    scope=fact_meta.get("scope", ""),
                    source_conversation_id=(
                        str(base_meta.get("channel_id"))
                        if base_meta.get("channel_id") else None
                    ),
                )
                if not indexed:
                    errors.append("fact_store")
    except Exception as e:
        log.warning("fact_store record failed (non-fatal): %s", e)
        errors.append("fact_store")
    return errors


async def _ingest_relations(user_id, relations: list[dict]) -> int:
    """Phase C-2: ingest entity-entity relations into the graph layer.

    Resolves src/dst canonical names via graph.find_entity (which knows
    about aliases & appellations) — only inserts a relation when both
    endpoints already exist as entities. We don't auto-create endpoints
    from relations to avoid letting a hallucinated edge spawn ghost
    entities. Returns the number of relations added; non-fatal on failure.
    """
    added = 0
    try:
        from . import graph
        for rel in relations:
            src_name = rel.get("src")
            rel_type = rel.get("rel_type")
            dst_name = rel.get("dst")
            if not (src_name and rel_type and dst_name):
                continue
            src_ent = await asyncio.to_thread(
                graph.find_entity, src_name, str(user_id), fuzzy=False,
            )
            dst_ent = await asyncio.to_thread(
                graph.find_entity, dst_name, str(user_id), fuzzy=False,
            )
            if not (src_ent and dst_ent):
                continue
            rid = await _write_thread(
                graph.add_relation,
                src_ent.id, rel_type, dst_ent.id, str(user_id),
                source_fact_id=rel.get("source_fact_id"), valid_from=rel.get("valid_from"),
                valid_to=rel.get("valid_to"),
            )
            if rid:
                added += 1
        if added:
            log.info("[REL_ADDED count=%d user=...%s]", added, str(user_id)[-4:])
    except Exception as exc:
        log.warning("relation ingest failed (non-fatal): %s", exc)
    return added


# ---------------------------------------------------------------------------
# Legacy path: Mem0 standard fact extraction (fallback)
# ---------------------------------------------------------------------------
async def _add_legacy(
    mem,
    messages: list[dict] | None,
    user_text: str,
    assistant_text: str,
    user_id,
    base_meta: dict,
) -> dict:
    """Let the memory store's built-in extractor handle the turn.

    context_hint is INTENTIONALLY excluded from legacy_msgs. The Phase 1
    root cause (other-speaker contamination) was the system-message
    channel-history path; we keep it sealed here regardless of which gate
    is enabled. The caller-supplied `messages` are also filtered to
    user/assistant roles only so a raw list with role=system can't
    re-introduce the contamination vector.
    """
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
    # LLM call on top of the store's own internal extractor. The gate path
    # is the canonical source of taxonomy metadata; in legacy fallback we
    # accept the simpler default and keep this path at 1 LLM call, not 2.
    result = await asyncio.to_thread(
        mem.add, legacy_msgs, user_id=user_id, metadata=base_meta,
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


# ---------------------------------------------------------------------------
# Conflict archive + deterministic attribute route
# ---------------------------------------------------------------------------
async def _archive_historical_observation(mem, fact_id, successor, user_id):
    """Keep a late observation as history without replacing its newer update.

    This is lineage/arrival reconciliation, not inferred world-valid time.
    """
    target, kind = successor
    seen = {fact_id}
    for _ in range(32):
        if target in seen:
            return False
        seen.add(target)
        row = await asyncio.to_thread(mem.get, target)
        if not isinstance(row, dict):
            return False
        md = fact_metadata(row)
        owner = row.get("user_id") or md.get("user_id")
        if owner is not None and str(owner) != str(user_id):
            return False
        if md.get("valid_to"):
            target = md.get("superseded_by")
            if not target:
                return False
            continue
        return await _archive_outdated_facts(mem, [(fact_id, kind)], user_id=user_id,
                                             new_fact_id=target, candidate_metadata=md) == 1
    return False


async def _archive_outdated_facts(
    mem, archive: list[tuple[str, str]], *, user_id=None,
    new_fact_id: str | None = None, candidate_metadata: dict | None = None,
) -> int:
    """Archive only committed replacements; re-read under the shared writer lock."""
    from .reinforce import mutate_memory_metadata_sync
    success = 0
    # Within a batch the nearest committed predecessor is appended last.
    # Close it first so an earlier ancestor's existing chain can reach the
    # replacement when its idempotent repair is checked.
    for existing_id, kind in reversed(list(dict.fromkeys(archive))):
        try:
            def mutation(existing):
                md = fact_metadata(existing)
                owner = existing.get("user_id") or md.get("user_id")
                if user_id is not None and owner is not None and str(owner) != str(user_id):
                    return None
                if md.get("valid_to"):
                    # This is already retired. Never overwrite its original
                    # successor with a later concurrent writer's attribution.
                    return None
                if candidate_metadata is not None:
                    eligible = can_supersede(md, candidate_metadata)
                    if kind == "merge":
                        eligible = same_fact_domain(md, candidate_metadata)
                        from .attribute_registry import observation_time
                        old_time, new_time = observation_time(md), observation_time(candidate_metadata)
                        if old_time is not None and (new_time is None or new_time < old_time):
                            eligible = False
                    if not eligible:
                        return None
                patch = {"valid_to": int(time.time()), "conflict_archived_as": kind}
                if new_fact_id:
                    patch["superseded_by"] = new_fact_id
                return patch
            def record_retirement(row):
                md = row.get("metadata") or {}
                owner = row.get("user_id") or md.get("user_id")
                if user_id is not None and owner is not None and str(owner) != str(user_id):
                    return
                if md.get("valid_to") is not None:
                    from . import lineage
                    lineage.retire_source(existing_id, user_id=str(owner or ""), retired_at=md["valid_to"])
            updated = await _write_thread(mutate_memory_metadata_sync, mem, existing_id, mutation,
                                          after_update=record_retirement)
            if not updated:
                # A prior retry may have completed this very archive.
                old = await asyncio.to_thread(mem.get, existing_id)
                md = (old.get("metadata") or {}) if isinstance(old, dict) else {}
                if not md.get("valid_to"):
                    continue
                if new_fact_id and md.get("superseded_by") != new_fact_id:
                    if not await _archive_chain_reaches(mem, existing_id, new_fact_id,
                                                       user_id, candidate_metadata or {}):
                        continue
            from . import graph
            if not await _write_thread(graph.expire_relations_for_fact, existing_id):
                continue
            if settings.MEM0_FACT_STORE_ENABLED:
                from . import fact_store
                projection_successor = new_fact_id if updated else md.get("superseded_by")
                await _write_thread(fact_store.supersede_fact, existing_id, projection_successor)
            success += 1
        except Exception as exc:
            log.warning("conflict archive failed (%s): %s", existing_id, exc)
    return success


async def _archive_chain_reaches(mem, start, target, user_id, candidate_metadata):
    """An already retired ancestor can satisfy repair without rewriting lineage."""
    seen = set()
    current = start
    expected = fact_metadata({"metadata": candidate_metadata})
    for _ in range(64):
        if not current or current in seen:
            return False
        seen.add(current)
        row = await asyncio.to_thread(mem.get, current)
        if not isinstance(row, dict):
            return False
        md = fact_metadata(row)
        owner = row.get("user_id") or md.get("user_id")
        if user_id is not None and str(owner) != str(user_id):
            return False
        if (md.get("scope") != expected.get("scope")
                or (md.get("condition") or "") != (expected.get("condition") or "")
                or md.get("valid_from") != expected.get("valid_from")):
            return False
        if md.get("attribute") and expected.get("attribute") and md["attribute"] != expected["attribute"]:
            return False
        if current == target:
            return True
        if not md.get("valid_to"):
            return False
        current = md.get("superseded_by")
    return False

async def _get_facts_by_attribute(mem, user_id, attribute: str) -> list[dict]:
    """Phase A-3: attribute 完全一致の valid な既存 fact を取得。

    v5 T8a: MEM0_FACT_STORE_ENABLED なら SQL 索引 (fact_store) を一次に使い、
    行を Chroma 互換 shape ({"id", "memory", "metadata"}) へ変換して返す。
    store が空/未整備なら従来の Chroma 経路へフォールバック（漏れ防止）。

    Chroma 経路: get_episode_facts(search.py) と同じく filters を試し、SDK
    非対応(TypeError) なら全件取得 + Python フィルタにフォールバック。
    valid_to 済み(soft-archive)は除外する。
    """
    if not attribute:
        return []
    # A partial side index cannot prove completeness. Read authoritative
    # metadata until a durable projection watermark is available.
    from .client import _rejects_user_id_kwarg, mem_get_all
    try:
        raw = await asyncio.to_thread(
            mem.get_all, user_id=user_id,
            filters={"attribute": attribute}, limit=None,
        )
    except Exception as exc:
        if not (isinstance(exc, TypeError) or _rejects_user_id_kwarg(exc)):
            log.warning("attr conflict get_all failed: %s", exc)
            raise RuntimeError("attribute_lookup_unavailable") from exc
        # SDK がこの呼び形を受けない（旧SDK: filters 非対応 / 新SDK:
        # トップレベル user_id 非対応）→ user 全件 + Python フィルタ
        try:
            raw = await asyncio.to_thread(
                mem_get_all, mem, user_id=user_id, limit=None)
        except Exception as exc2:
            log.warning("attr conflict get_all fallback failed: %s", exc2)
            raise RuntimeError("attribute_lookup_unavailable") from exc2
    rows = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    now = int(time.time())
    out: list[dict] = []
    for r in rows:
        md = r.get("metadata") or {}
        if md.get("attribute") != attribute:
            continue
        vt = md.get("valid_to")
        if vt:
            try:
                if int(vt) <= now:
                    continue
            except (TypeError, ValueError):
                pass
        out.append(r)
    return out


def _compare_typed_values_shadow(ex: dict, cand: dict, attr: str) -> None:
    """v5 T7: 数値属性のシャドー比較ログ（挙動には影響しない）。

    既存 fact の metadata.value_json と候補 fact の value が両方 quantity で
    単位が一致するとき、same/changed を [ATTR_VALUE_COMPARE] で記録する。
    単位が違う場合は unit_mismatch（換算は将来。今は観測のみ）。
    """
    try:
        import json
        cand_val = cand.get("value")
        if not isinstance(cand_val, dict) or cand_val.get("type") != "quantity":
            return
        raw = (ex.get("metadata") or {}).get("value_json")
        if not raw:
            return
        ex_val = json.loads(raw)
        if not isinstance(ex_val, dict) or ex_val.get("type") != "quantity":
            return
        if ex_val.get("unit") != cand_val.get("unit"):
            verdict = "unit_mismatch"
        elif float(ex_val.get("value")) == float(cand_val.get("value")):
            verdict = "same"
        else:
            verdict = "changed"
        log.info(
            "[ATTR_VALUE_COMPARE attribute=%s verdict=%s existing=%s%s "
            "candidate=%s%s]",
            attr, verdict,
            ex_val.get("value"), ex_val.get("unit"),
            cand_val.get("value"), cand_val.get("unit"),
        )
    except Exception as exc:  # noqa: BLE001 - 観測用、絶対に本流を壊さない
        log.debug("typed value compare failed (non-fatal): %s", exc)


async def _attribute_conflict_check(
    mem, user_id, kept: list[dict], *, exclude_ids: set,
    base_meta: dict | None = None,
) -> list[tuple[str, str]]:
    """Phase A-3: 決定的 conflict ルート（embedding 距離に依存しない）。

    kept の各 fact の attribute で既存 valid fact を metadata 照会し、
    classify_conflict が update/correction を返したら (existing_id, ctype) を
    集めて返す。dedup が既に処理した existing_id は exclude_ids で除く。
    失敗は非致命(空を返す) — 新 fact は既に保存されるので archive は best-effort。
    """
    from .conflict import classify_conflict
    from .attribute_registry import (
        conditions_are_conflict_candidate, get_conflict_group,
        is_update_cardinality,
    )
    out: list[tuple[str, str]] = []
    seen = set(exclude_ids)
    for f in kept:
        attr = f.get("attribute") or ""
        if not attr:
            continue
        cand_text = f.get("text") or ""
        if not cand_text:
            continue
        # v5 T4: 同一 attribute だけでなく conflict group のメンバー全員を照会
        # (likes_coffee vs avoids_caffeine のような cross-attribute 矛盾)。
        # group 未定義なら {attr} 単独で従来挙動。
        existing: list[dict] = []
        try:
            for group_attr in sorted(get_conflict_group(attr)):
                existing.extend(
                    await _get_facts_by_attribute(mem, user_id, group_attr)
                )
        except Exception as exc:
            raise RuntimeError("attribute_lookup_unavailable") from exc
        for ex in existing:
            eid = ex.get("id")
            if not eid or eid in seen:
                continue
            ex_text = ex.get("memory") or ex.get("text") or ""
            if not ex_text or ex_text == cand_text:
                continue
            old_md, new_md = fact_metadata(ex), fact_metadata(f, base_meta)
            if not can_supersede(old_md, new_md):
                continue
            # Phase B+/v5: condition 互換判定。disjoint(別条件下=朝の habit vs
            # 夜の habit)なら矛盾ではないので archive しない。same/subset/
            # superset/unknown は重なりうるので conflict 判定へ進める。
            ex_cond = (ex.get("metadata") or {}).get("condition") or ""
            cand_cond = f.get("condition") or ""
            if not conditions_are_conflict_candidate(ex_cond, cand_cond):
                continue
            # v5 T7: typed value のシャドー数値比較。両者が quantity なら結果を
            # ログに残す（classify_conflict の判定と突き合わせて精度を観測し、
            # 将来 LLM 判定の置換可否を決める。今は挙動に影響させない）。
            _compare_typed_values_shadow(ex, f, attr)
            try:
                cls = await classify_conflict(ex_text, cand_text,
                                              existing_metadata=old_md,
                                              candidate_metadata=new_md)
            except Exception as exc:
                raise RuntimeError("conflict_classification_unavailable") from exc
            if cls.get("retryable"):
                raise RuntimeError("conflict_classification_unavailable")
            ctype = cls.get("conflict_type", "addition")
            # v5: cardinality が set_like の attribute は追加型。update 判定でも
            # archive しない(既存の趣味・好みを新しい追加で消さない)。判断基準は
            # 「消される側=既存 fact」の attribute (group 越しで候補と異なりうる)。
            # 無ければ候補側にフォールバック。
            ex_attr = (ex.get("metadata") or {}).get("attribute") or attr
            from .attribute_registry import explicit_member_transition
            if ctype in ("update", "correction") and (is_update_cardinality(ex_attr) or explicit_member_transition(old_md, new_md)):
                log.info(
                    "[ATTR_CONFLICT attribute=%s conflict_type=%s existing_id=%s "
                    "cand=%r]",
                    attr, ctype, eid, cand_text[:60],
                )
                out.append((eid, ctype))
                seen.add(eid)
    return out

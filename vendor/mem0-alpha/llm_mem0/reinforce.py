"""Hebbian-style reinforcement: when a duplicate fact is detected, bump
the existing memory's `mention_count` instead of dropping it silently.

This is the read-modify-write helper called from the ``ingest`` module when
``check_near_dup`` returns ``is_dup=True``. Mem0's ``mem.update()`` requires
the ``data`` argument, so we re-pass the existing memory text unchanged.

Field semantics (added to fact.metadata):
    mention_count        int   times this fact was reinforced (incl. first ingest)
    recall_count         int   times this fact was surfaced by search (testing effect)
    reinforced_at        list  unix-seconds timestamps of the most recent N presentations
    last_reinforced_at   int   most recent unix-seconds timestamp
    last_recalled_at     int   most recent recall-driven bump (rate-limit anchor)

``importance`` is deliberately NOT touched here (bugfix — an earlier
version bumped it +1 per reinforcement, capped at 5). It is an LLM-assigned
judgment of how load-bearing the fact is (set once at extraction time), not
a measure of how often it has been mentioned. Bumping it on every
reinforcement inflated trivial facts (a hobby mentioned 3 times) into
importance=5 "core memory" status (see search.get_core_memories), crowding
out facts that are actually irreplaceable. Frequency/recency belong in
mention_count / recall_count / reinforced_at and are read by
reinforcement_boost() below — importance and "familiarity" are different
axes even in human memory (a well-rehearsed trivial fact isn't thereby
more *important*).

Metadata mutations are serialized across threads and cooperating processes
sharing STATE_DIR. Writers must use the same mutation helper to participate.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import hashlib
import json
import threading
from pathlib import Path
from contextlib import contextmanager

from .settings import settings

log = logging.getLogger(__name__)

# Public so callers / tests can introspect the schema.
MAX_REINFORCED_HISTORY = 10
# The importance scale cap (1-5). Reinforcement no longer writes importance
# (see module docstring); the constant stays for callers that reference the
# scale itself.
MAX_IMPORTANCE = 5

# Import-time snapshot for introspection/back-compat; the live value is
# settings.MEM0_RECALL_REINFORCE_MIN_INTERVAL_SEC (read per call).
RECALL_REINFORCE_MIN_INTERVAL_SEC = settings.MEM0_RECALL_REINFORCE_MIN_INTERVAL_SEC

_MUTATION_LOCKS = [threading.RLock() for _ in range(128)]


@contextmanager
def memory_mutation_lock(memory_id: str):
    """Bounded lock files; never hold this synchronous lock on the event loop."""
    import fcntl
    stripe = int(hashlib.sha256(str(memory_id).encode()).hexdigest()[:8], 16) % 128
    directory = Path(settings.STATE_DIR) / "memory_mutation_locks"
    directory.mkdir(parents=True, exist_ok=True)
    with _MUTATION_LOCKS[stripe], (directory / f"{stripe}.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def mutate_memory_metadata_sync(mem, memory_id: str, mutation, *, after_update=None) -> bool:
    """Fresh read + metadata-preserving update under a shared writer lock.

    mutation receives the fresh row and returns a metadata patch or None.
    The SDK may replace metadata rather than merge it, so pass the full map.
    """
    with memory_mutation_lock(memory_id):
        existing = mem.get(memory_id)
        if not isinstance(existing, dict):
            return False
        text = existing.get("memory") or existing.get("text") or ""
        if not isinstance(text, str) or not text:
            return False
        partial = mutation(existing)
        if partial is None:
            if after_update is not None:
                after_update(existing)
            return False
        metadata = {**(existing.get("metadata") or {}), **partial}
        mem.update(memory_id=memory_id, data=text, metadata=metadata)
        if after_update is not None:
            after_update({**existing, "metadata": metadata})
        from .search import invalidate_core_cache
        invalidate_core_cache(existing.get("user_id") or metadata.get("user_id"))
        return True


def _reinforce_locked(mem, memory_id: str, *, recall: bool, event_id: str | None = None,
                      source_timestamp=None) -> bool:
    def mutation(existing):
        md = existing.get("metadata") or {}
        now = int(time.time())
        try:
            if md.get("valid_to") is not None and float(md["valid_to"]) <= now:
                return None
        except (TypeError, ValueError):
            return None
        events = md.get("reinforced_event_ids", "[]")
        try:
            events = json.loads(events) if isinstance(events, str) else events
        except (ValueError, TypeError):
            events = []
        events = events if isinstance(events, list) else []
        # The origin event is already counted by the original insertion.
        if event_id and (event_id in events or event_id in (md.get("request_id"), md.get("batch_id"))):
            return None
        if recall:
            try:
                last = int(md.get("last_recalled_at") or 0)
            except (ValueError, TypeError):
                last = 0
            if last and now - last < settings.MEM0_RECALL_REINFORCE_MIN_INTERVAL_SEC:
                return None
        field = "recall_count" if recall else "mention_count"
        try:
            count = max(0, int(md.get(field, 0 if recall else 1)))
        except (ValueError, TypeError):
            count = 0 if recall else 1
        history = md.get("reinforced_at")
        history = list(history) if isinstance(history, list) else []
        partial = {field: count + 1, "reinforced_at": (history + [now])[-MAX_REINFORCED_HISTORY:],
                   "last_reinforced_at": now}
        if recall:
            partial["last_recalled_at"] = now
        if event_id:
            partial["reinforced_event_ids"] = json.dumps((events + [event_id])[-128:])
        if not recall and source_timestamp is not None and not isinstance(source_timestamp, bool):
            from .attribute_registry import observation_time
            incoming = observation_time({"source_timestamp": source_timestamp})
            previous = observation_time(md)
            if incoming is not None:
                partial["user_confirmed_at"] = max(incoming, previous) if previous is not None else incoming
        return partial
    try:
        if not memory_id:
            return False
        from . import ingest_journal as journal
        from .search import invalidate_core_cache
        with memory_mutation_lock(memory_id):
            existing = mem.get(memory_id)
            if not isinstance(existing, dict):
                return False
            # Finish earlier planned confirmations before a different event
            # can reuse the same revision. The journal is authoritative; the
            # bounded metadata list is only recent provenance.
            if event_id and not recall:
                for pending_id, revision, partial in journal.pending_events(memory_id):
                    md = existing.get("metadata") or {}
                    if int(md.get("confirmation_revision") or 0) < revision:
                        if int(md.get("confirmation_revision") or 0) + 1 != revision:
                            raise RuntimeError("confirmation_revision_gap")
                        if md.get("valid_to") is not None and float(md["valid_to"]) <= time.time():
                            return False
                        text = existing.get("memory") or existing.get("text") or ""
                        mem.update(memory_id=memory_id, data=text, metadata={**md, **partial})
                        existing = mem.get(memory_id)
                        if not isinstance(existing, dict) or int((existing.get("metadata") or {}).get("confirmation_revision") or 0) < revision:
                            raise RuntimeError("confirmation_update_unconfirmed")
                    journal.finish_event(memory_id, pending_id)
                if journal.event_recorded(memory_id, event_id):
                    return False
            partial = mutation(existing)
            if partial is None:
                if event_id and not recall:
                    md = existing.get("metadata") or {}
                    events = md.get("reinforced_event_ids", "[]")
                    events = json.loads(events) if isinstance(events, str) else events
                    if event_id in (events if isinstance(events, list) else []) or event_id in (md.get("request_id"), md.get("batch_id")):
                        journal.plan_event(memory_id, event_id, int(md.get("confirmation_revision") or 0), {}, applied=True)
                return False
            text = existing.get("memory") or existing.get("text") or ""
            if not isinstance(text, str) or not text:
                return False
            md = existing.get("metadata") or {}
            if event_id and not recall:
                revision = int(md.get("confirmation_revision") or 0) + 1
                partial["confirmation_revision"] = revision
                journal.plan_event(memory_id, event_id, revision, partial)
            mem.update(memory_id=memory_id, data=text, metadata={**md, **partial})
            if event_id and not recall:
                confirmed = mem.get(memory_id)
                if not isinstance(confirmed, dict) or int((confirmed.get("metadata") or {}).get("confirmation_revision") or 0) != revision:
                    raise RuntimeError("confirmation_update_unconfirmed")
                journal.finish_event(memory_id, event_id)
            invalidate_core_cache(existing.get("user_id") or md.get("user_id"))
            return True
    except Exception as exc:
        log.warning("reinforcement failed (%s): %s", memory_id, exc)
        return False


def reinforce_existing_sync(mem, memory_id: str, *, event_id: str | None = None, source_timestamp=None) -> bool:
    """Serialize mention reinforcement; redelivery of an event is a no-op."""
    return _reinforce_locked(mem, memory_id, recall=False, event_id=event_id, source_timestamp=source_timestamp)

async def reinforce_existing(mem, memory_id: str, *, event_id: str | None = None, source_timestamp=None) -> bool:
    return await asyncio.to_thread(reinforce_existing_sync, mem, memory_id, event_id=event_id, source_timestamp=source_timestamp)

# ---------------------------------------------------------------------------
# Recall-time reinforcement (testing effect) — Phase ①
# ---------------------------------------------------------------------------
# Minimum spacing between two recall-driven bumps of the SAME fact, mirroring
# the spacing effect: massed (back-to-back) retrieval practice gives sharply
# diminishing returns in humans, and without this a single hot query loop
# would runaway-boost a handful of facts on every turn. Distinct from
# reinforce_existing's mention_count channel, which fires on ingest-time
# near-dup absorption and is not rate-limited (dedup hits are naturally rare
# and each represents a genuinely new utterance).


def reinforce_on_recall_sync(mem, memory_id: str) -> bool:
    """Atomically enforce recall spacing and preserve unrelated metadata."""
    return _reinforce_locked(mem, memory_id, recall=True)

async def reinforce_on_recall(mem, memory_id: str) -> bool:
    """Async wrapper around reinforce_on_recall_sync."""
    return await asyncio.to_thread(reinforce_on_recall_sync, mem, memory_id)


def _legacy_reinforcement_boost(metadata: dict, *,
                                alpha_mention: float,
                                alpha_recency: float,
                                recency_half_life_days: float,
                                alpha_age: float | None,
                                age_half_life_days: float | None) -> float:
    """Original hand-tuned three-term boost (frequency + recency + age).

    Combines three components:
      - frequency: ``alpha_mention * log(mention_count)`` (log dampening so
        1→10 mentions has the same delta as 10→100; mention=1 yields 0)
      - recency: ``alpha_recency * exp(-days_since_last_reinforce/HL)``
        (boost decays after Hebbian reinforcement falls silent)
      - age (Phase A-2): ``alpha_age * exp(-days_since_creation/HL_age)``
        (new facts get a small bump so a state change like "髪色 = 黒 →
        ハイトーン" is preferred over the older row at retrieval time)

    Kept as the MEM0_BOOST_MODE=legacy path (also the default) so the ACT-R
    mode (below) can be A/B compared against known-good behavior before it
    becomes the default.
    """
    try:
        mention = int(metadata.get("mention_count", 1))
    except (TypeError, ValueError):
        mention = 1
    try:
        last_r = int(metadata.get("last_reinforced_at") or 0)
    except (TypeError, ValueError):
        last_r = 0

    boost = alpha_mention * math.log(mention) if mention > 1 else 0.0
    if last_r:
        days_since = max(0.0, (time.time() - last_r) / 86400.0)
        boost += alpha_recency * math.exp(-days_since / recency_half_life_days)

    # Phase A-2: age decay. ingest stamps `timestamp` (unix seconds) on
    # every new fact via base_meta; created_at_unix is accepted as an alias
    # for any caller that writes it under that name.
    aa = settings.MEM0_BOOST_ALPHA_AGE if alpha_age is None else alpha_age
    ah = (settings.MEM0_BOOST_AGE_HALF_LIFE_DAYS
          if age_half_life_days is None else age_half_life_days)
    if aa and ah > 0:
        try:
            created = int(metadata.get("created_at_unix") or metadata.get("timestamp") or 0)
        except (TypeError, ValueError):
            created = 0
        if created:
            days_since_creation = max(0.0, (time.time() - created) / 86400.0)
            boost += aa * math.exp(-days_since_creation / ah)
    return boost


# ---------------------------------------------------------------------------
# ACT-R base-level activation — Phase ②
# ---------------------------------------------------------------------------
# ACT-R decay exponent (Anderson & Schooler 1991 / Anderson et al. 2004 use
# ~0.5 as the canonical default fit to human free-recall data). Frequency and
# recency fall out of ONE formula here instead of being two separately
# hand-tuned terms like the legacy path above — that's the point of using a
# power-law (not exponential) decay: human forgetting curves are well-fit by
# a power law, exponential decay systematically under-predicts long-term
# retention of well-rehearsed items.
#
# Knobs (read dynamically from settings):
#   MEM0_ACTR_DECAY_D              decay exponent d (default 0.5)
#   MEM0_ACTR_BOOST_ALPHA          max boost the logistic can emit (0.2)
#   MEM0_ACTR_RETRIEVAL_THRESHOLD  logistic center; calibrated to sit inside
#                                  the realistic activation range (-7.5,
#                                  matching decay.py's Hot cutoff)
#   MEM0_ACTR_NOISE_S              logistic temperature (1.0)
#
# Floor on (now - t) before exponentiating — guards against a zero/negative
# delta (clock skew, or a reinforcement landing in the same wall-clock second
# as `now`) blowing up to +inf under a negative exponent.
_ACTR_MIN_DT_SEC = 1.0


def _actr_activation(
    created_at: int, reinforced_at: list, mention_count: int, recall_count: int,
    *, now: float, d: float = 0.5,
) -> float:
    """ACT-R base-level activation: ``B = ln( Σ_j (now - t_j)^-d )``.

    ``reinforced_at`` holds only the most recent ``MAX_REINFORCED_HISTORY``
    (10) presentation timestamps — older ones are evicted to keep metadata
    small. When the true total presentation count (``mention_count +
    recall_count``) exceeds what's in history, the exact times of the
    evicted presentations are gone, so we approximate their contribution
    (Petrov 2006's approach to bounded-history activation): treat the
    missing presentations as uniformly distributed between the fact's
    creation time and the oldest timestamp still in history, and integrate
    the decay function over that interval rather than guessing individual
    timestamps.

    Returns ``-inf`` for a fact with zero valid presentation timestamps
    (should not happen in practice — creation always stamps ``timestamp``).
    """
    known = sorted(t for t in (reinforced_at or []) if isinstance(t, (int, float)) and t > 0)
    times = ([created_at] if created_at else []) + known
    times = sorted(t for t in times if t > 0)
    if not times:
        return float("-inf")

    def decay(dt: float) -> float:
        return max(dt, _ACTR_MIN_DT_SEC) ** (-d)

    total = sum(decay(now - t) for t in times)

    n_total = max(int(mention_count or 0), 1) + max(int(recall_count or 0), 0)
    missing = n_total - len(times)
    if missing > 0 and len(times) >= 2:
        # Spread the `missing` evicted presentations uniformly across the
        # oldest known gap [t0, t1] and integrate the decay curve over it,
        # rather than concentrating them at either endpoint.
        t0, t1 = times[0], times[1]
        span = max(t1 - t0, 1.0)
        dt0 = max(now - t0, _ACTR_MIN_DT_SEC)
        dt1 = max(now - t1, _ACTR_MIN_DT_SEC)
        if abs(d - 1.0) > 1e-9:
            integral = (dt0 ** (1 - d) - dt1 ** (1 - d)) / (1 - d)
        else:
            integral = math.log(dt0) - math.log(dt1)
        total += missing * max(integral / span, 0.0)
    elif missing > 0:
        # Only one known timestamp (shouldn't normally happen — created_at
        # is always stamped): fall back to a conservative floor rather than
        # extrapolating past a single data point.
        total += missing * decay(now - times[0])

    if total <= 0:
        return float("-inf")
    return math.log(total)


def _actr_reinforcement_boost(metadata: dict) -> float:
    """ACT-R-activation-based boost. See MEM0_BOOST_MODE in reinforcement_boost.

    Maps raw activation into a (0, alpha) boost via ACT-R's own retrieval-
    probability logistic (Anderson & Lebiere 1998): P(recall) =
    sigmoid((B - threshold) / noise). This is NOT a plain sigmoid(B) — with
    d=0.5, activation for a realistic fact is deeply negative (-5..-9 is
    typical for a once-mentioned fact aged days to a year), so sigmoid(B)
    alone would flatten every real fact to ~0 boost. Centering the logistic
    at the retrieval threshold spreads the boost meaningfully across facts
    a corpus actually contains.
    """
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

    activation = _actr_activation(
        created, reinforced_at, mention, recall,
        now=time.time(), d=settings.MEM0_ACTR_DECAY_D,
    )
    if activation == float("-inf"):
        return 0.0
    threshold = settings.MEM0_ACTR_RETRIEVAL_THRESHOLD
    noise = settings.MEM0_ACTR_NOISE_S
    p_recall = 1.0 / (1.0 + math.exp(-(activation - threshold) / noise))
    return settings.MEM0_ACTR_BOOST_ALPHA * p_recall


def reinforcement_boost(metadata: dict | None, *,
                        alpha_mention: float = 0.10,
                        alpha_recency: float = 0.05,
                        recency_half_life_days: float = 30.0,
                        alpha_age: float | None = None,
                        age_half_life_days: float | None = None) -> float:
    """Return the additive score boost for a memory.

    Dispatches on ``settings.MEM0_BOOST_MODE`` (default ``legacy``):
      - ``legacy``: the original hand-tuned frequency + recency + age terms
        (``_legacy_reinforcement_boost``).
      - ``actr``: a single power-law base-level-activation formula that
        subsumes both frequency and recency (``_actr_reinforcement_boost``)
        — see that function's docstring. Human forgetting is well-fit by a
        power law; exponential decay (the legacy recency/age terms) is a
        reasonable local approximation but under-predicts retention of
        well-rehearsed old facts and over-predicts retention of
        rarely-touched recent ones.

    Used by ``search._apply_reinforcement_boost``, which subtracts the
    returned boost from the candidate's cosine distance — a positive boost
    moves a memory closer (more likely to be retrieved / rerank-selected).
    """
    if not metadata:
        return 0.0
    if settings.MEM0_BOOST_MODE == "actr":
        return _actr_reinforcement_boost(metadata)
    return _legacy_reinforcement_boost(
        metadata,
        alpha_mention=alpha_mention,
        alpha_recency=alpha_recency,
        recency_half_life_days=recency_half_life_days,
        alpha_age=alpha_age,
        age_half_life_days=age_half_life_days,
    )

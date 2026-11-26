"""Retrieval v1 — fused direct list + associative extras (pure logic, no I/O).

Selected configuration (assoc-eval 2026-10-15: arm B1 for the direct list,
arms A_tag + A_ent for the associates):

Direct list
  * reciprocal-rank fusion (``RRF_K`` = 60) SUMMED over every query variant's
    vector ranking (top ``TOPN`` = 20 valid hits each) and ONE BM25 ranking of
    the searched query string (top 20 valid hits); no placeholder distances,
    no graph siblings.
  * soft gate: drop a candidate whose best vector cosine (max over variants,
    ``cos = 1 - d/2`` from the store's squared-L2 distance) is below 0.40
    unless its BM25 rank is <= 2. (1 - d/2 is the cosine of unit vectors; the
    stored embeddings' norms are 1 +- 7e-4, so a value within ~1e-3 of 0.40
    can land on the other side of the normalised cosine the evaluation used.)
  * order: (-rrf, -best_cos, id).

Associates (extra slots after the direct list, never displacing it)
  * seeds = the first ``N_SEEDS`` (3) gated non-core candidates, weighted by
    their RRF score normalised over the seeds.
  * tag signal: shared NFKC+lower tags; only tags with 2 <= df <= 50 over the
    user's live facts count, idf = ln((N+1)/(df+1)).
  * entity signal: shared graph entities; hubs with live fan > 50 are skipped,
    specificity = ln((N+1)/(fan+1)) / ln(N+1).
  * candidates = top-2 by tag score U top-2 by entity score (ties broken by
    qcos desc, then id asc), excluding direct, core, seed and non-live facts;
    keep qcos >= 0.40 (cosine of query and candidate embeddings) and append
    up to 2 by qcos descending (ties: id descending). Gate and order compare
    qcos at 4 decimals, as the evaluated selection did.

This module performs no I/O and imports nothing from
``services.mem0.search`` so it can be vendored into llm_mem0 as is.
"""

from __future__ import annotations

import math
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np

RRF_K = 60
TOPN = 20
N_SEEDS = 3
TAG_DF_MIN = 2
TAG_DF_MAX = 50
ENT_FAN_MAX = 50
PER_SIGNAL = 2
DIRECT_COS_MIN = 0.40
DIRECT_BM25_BYPASS_RANK = 2
ASSOC_QCOS_MIN = 0.40
ASSOC_MAX = 2
QCOS_NDIGITS = 4
INDEX_TTL_S = 600.0

_NEVER = np.iinfo(np.int64).max


# ---------------------------------------------------------------------------
# Direct list: fusion + soft gate
# ---------------------------------------------------------------------------
def cos_from_distance(distance: float) -> float:
    """Cosine from the store's squared-L2 distance (unit vectors: d = 2 - 2cos)."""
    return 1.0 - float(distance) / 2.0


@dataclass
class Fused:
    """One fused candidate of the direct list."""

    fid: str
    rrf: float = 0.0
    vec_ranks: dict[int, int] = field(default_factory=dict)
    bm25_rank: int | None = None
    best_cos: float | None = None

    @property
    def best_distance(self) -> float | None:
        return None if self.best_cos is None else 2.0 - 2.0 * self.best_cos


def fuse(
    vector_lists: Sequence[Sequence[tuple[str, float | None]]],
    bm25_ids: Sequence[str],
    *,
    k: int = RRF_K,
    topn: int = TOPN,
) -> dict[str, Fused]:
    """RRF over every variant's vector ranking plus one BM25 ranking.

    ``vector_lists[v]`` holds variant v's valid hits as ``(fact_id, distance)``
    in ascending distance order. Only the first ``topn`` contribute RRF; the
    whole list contributes cosine knowledge (``best_cos``) for candidates that
    entered the fusion through any ranking. ``bm25_ids`` is the valid BM25
    ranking (rank 1 first); only its first ``topn`` contribute.
    """
    out: dict[str, Fused] = {}
    for v, hits in enumerate(vector_lists):
        seen: set[str] = set()
        rank = 0
        for fid, _dist in hits:
            if not fid or fid in seen:
                continue
            seen.add(fid)
            rank += 1
            if rank > topn:
                break
            c = out.get(fid)
            if c is None:
                c = out[fid] = Fused(fid)
            c.rrf += 1.0 / (k + rank)
            c.vec_ranks[v] = rank
    rank = 0
    seen = set()
    for fid in bm25_ids:
        if not fid or fid in seen:
            continue
        seen.add(fid)
        rank += 1
        if rank > topn:
            break
        c = out.get(fid)
        if c is None:
            c = out[fid] = Fused(fid)
        c.rrf += 1.0 / (k + rank)
        c.bm25_rank = rank
    for hits in vector_lists:
        for fid, dist in hits:
            c = out.get(fid)
            if c is None or not isinstance(dist, (int, float)) or isinstance(dist, bool):
                continue
            cos = cos_from_distance(dist)
            if c.best_cos is None or cos > c.best_cos:
                c.best_cos = cos
    return out


def order_fused(cands: Iterable[Fused]) -> list[Fused]:
    """(-rrf, -best_cos, id); a candidate without any known cosine sorts last among ties."""
    return sorted(
        cands,
        key=lambda c: (-c.rrf, -(c.best_cos if c.best_cos is not None else -math.inf), c.fid),
    )


def passes_gate(
    c: Fused,
    *,
    cos_min: float = DIRECT_COS_MIN,
    bypass_rank: int = DIRECT_BM25_BYPASS_RANK,
) -> bool:
    if c.best_cos is not None and c.best_cos >= cos_min:
        return True
    return c.bm25_rank is not None and c.bm25_rank <= bypass_rank


def gate(
    ordered: Sequence[Fused],
    *,
    exclude: Iterable[str] = (),
    slots: int,
    cos_min: float = DIRECT_COS_MIN,
    bypass_rank: int = DIRECT_BM25_BYPASS_RANK,
) -> tuple[list[Fused], int]:
    """Apply the soft gate in fused order, skipping ``exclude`` (core facts).

    Returns ``(kept, drops)``: every passing candidate in order (the direct
    list is ``kept[:slots]``, the seeds ``kept[:N_SEEDS]``) and how many
    gate drops happened while the direct list was still being filled.
    """
    skip = set(exclude)
    kept: list[Fused] = []
    drops = 0
    for c in ordered:
        if c.fid in skip:
            continue
        if passes_gate(c, cos_min=cos_min, bypass_rank=bypass_rank):
            kept.append(c)
        elif len(kept) < slots:
            drops += 1
    return kept, drops


def seed_weights(kept: Sequence[Fused], n: int = N_SEEDS) -> list[tuple[str, float]]:
    """Seeds = the first ``n`` gated candidates, weights = RRF normalised over them."""
    seeds = list(kept[:n])
    total = sum(c.rrf for c in seeds)
    if total <= 0:
        return []
    return [(c.fid, c.rrf / total) for c in seeds]


# ---------------------------------------------------------------------------
# Association index (tags + graph entities + validity), per user
# ---------------------------------------------------------------------------
def norm_tags(raw) -> frozenset[str]:
    """Comma-separated tag string (or list of strings) -> NFKC+lower tag set."""
    if isinstance(raw, str):
        parts: Iterable = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        parts = (x for x in raw if isinstance(x, str))
    else:
        return frozenset()
    out = set()
    for x in parts:
        x = unicodedata.normalize("NFKC", x).strip().lower()
        if x:
            out.add(x)
    return frozenset(out)


def _valid_to(md: Mapping) -> int:
    """Same reading as search._is_fact_valid: falsy/unparsable valid_to = never expires."""
    vt = md.get("valid_to")
    if not vt:
        return int(_NEVER)
    try:
        return int(vt)
    except (TypeError, ValueError):
        return int(_NEVER)


_EMPTY: Mapping = {}


class AssocIndex:
    """Immutable per-user snapshot used for association scoring.

    Built from the user's facts (mem0 ``get_all`` rows: ``id`` + ``metadata``)
    and the graph's ``(fact_id, entity_id)`` edges (keyed by the store id, or
    by ``metadata.canonical_id`` under the managed integrity modes). Liveness
    is evaluated at query time from ``valid_to`` so a fact expiring inside the
    TTL drops out.
    """

    __slots__ = ("ids", "pos", "valid_to", "tags", "tag_post", "fact_ents", "ent_post", "built_at")

    def __init__(
        self,
        facts: Iterable[Mapping],
        edges: Iterable[tuple[str, int]] = (),
        *,
        built_at: float = 0.0,
    ) -> None:
        ids: list[str] = []
        pos: dict[str, int] = {}
        canonical: dict[str, int] = {}
        vto: list[int] = []
        tags: list[frozenset[str]] = []
        for row in facts:
            fid = str(row.get("id") or "") if isinstance(row, Mapping) else ""
            if not fid or fid in pos:
                continue
            md = row.get("metadata")
            md = md if isinstance(md, Mapping) else _EMPTY
            pos[fid] = len(ids)
            if md.get("canonical_id"):
                canonical.setdefault(str(md["canonical_id"]), len(ids))
            ids.append(fid)
            vto.append(_valid_to(md))
            tags.append(norm_tags(md.get("tags")))
        self.ids = ids
        self.pos = pos
        self.valid_to = np.asarray(vto, dtype=np.int64)
        self.tags = tags
        post: dict[str, list[int]] = {}
        for i, ts in enumerate(tags):
            for tg in ts:
                post.setdefault(tg, []).append(i)
        self.tag_post = {t: np.asarray(p, dtype=np.int64) for t, p in post.items()}
        f_ents: dict[int, set[int]] = {}
        e_post: dict[int, set[int]] = {}
        for fid, eid in edges:
            i = pos.get(str(fid))
            if i is None:
                i = canonical.get(str(fid))
            if i is None or eid is None:
                continue
            eid = int(eid)
            f_ents.setdefault(i, set()).add(eid)
            e_post.setdefault(eid, set()).add(i)
        self.fact_ents = {i: tuple(sorted(es)) for i, es in f_ents.items()}
        self.ent_post = {e: np.asarray(sorted(p), dtype=np.int64) for e, p in e_post.items()}
        self.built_at = float(built_at)

    def __len__(self) -> int:
        return len(self.ids)

    def __contains__(self, fid: object) -> bool:
        return fid in self.pos

    def live_mask(self, now: int, *, hide_archived: bool = True) -> np.ndarray:
        if not hide_archived:
            return np.ones(len(self.ids), dtype=bool)
        return self.valid_to > int(now)


# ---------------------------------------------------------------------------
# Association scoring + selection
# ---------------------------------------------------------------------------
def tag_scores(
    index: AssocIndex,
    seeds: Sequence[tuple[str, float]],
    *,
    exclude: Iterable[str],
    live: np.ndarray,
    seed_tags: Mapping[str, frozenset[str]] | None = None,
    df_min: int = TAG_DF_MIN,
    df_max: int = TAG_DF_MAX,
) -> dict[str, float]:
    """Σ_seeds w · idf over shared tags with df_min <= live df <= df_max."""
    n = int(live.sum())
    excl = {index.pos[f] for f in exclude if f in index.pos}
    acc: dict[int, float] = {}
    for fid, w in seeds:
        i = index.pos.get(fid)
        tags = index.tags[i] if i is not None else (seed_tags or {}).get(fid, frozenset())
        for tg in sorted(tags):
            p = index.tag_post.get(tg)
            if p is None:
                continue
            lp = p[live[p]]
            df = int(lp.size)
            if df < df_min or df > df_max:
                continue
            idf = math.log((n + 1) / (df + 1))
            for gi in lp.tolist():
                if gi in excl:
                    continue
                acc[gi] = acc.get(gi, 0.0) + w * idf
    return {index.ids[i]: s for i, s in acc.items()}


def entity_scores(
    index: AssocIndex,
    seeds: Sequence[tuple[str, float]],
    *,
    exclude: Iterable[str],
    live: np.ndarray,
    fan_max: int = ENT_FAN_MAX,
) -> dict[str, float]:
    """Σ_seeds w · specificity over shared entities whose live fan <= fan_max."""
    n = int(live.sum())
    ln_n = math.log(n + 1)
    if ln_n <= 0:
        return {}
    excl = {index.pos[f] for f in exclude if f in index.pos}
    acc: dict[int, float] = {}
    for fid, w in seeds:
        i = index.pos.get(fid)
        if i is None:
            continue
        for eid in index.fact_ents.get(i, ()):
            pf = index.ent_post[eid]
            lpf = pf[live[pf]]
            fan = int(lpf.size)
            if fan > fan_max:
                continue
            v = math.log((n + 1) / (fan + 1)) / ln_n
            for gi in lpf.tolist():
                if gi in excl:
                    continue
                acc[gi] = acc.get(gi, 0.0) + w * v
    return {index.ids[i]: s for i, s in acc.items()}


def tie_group(scores: Mapping[str, float], m: int = PER_SIGNAL) -> list[str]:
    """Every key scoring >= the m-th best score: the set whose top-m needs the qcos tie-break."""
    if not scores or m <= 0:
        return []
    vals = sorted(scores.values(), reverse=True)
    thr = vals[min(m, len(vals)) - 1]
    return sorted(k for k, v in scores.items() if v >= thr)


def top_by_score(scores: Mapping[str, float], qcos: Mapping[str, float], m: int = PER_SIGNAL) -> list[str]:
    """Top-m by (-score, -qcos, id) among candidates that still have an embedding."""
    cands = [k for k in scores if k in qcos]
    return sorted(cands, key=lambda k: (-scores[k], -qcos[k], k))[:max(m, 0)]


def select_associates(
    tag: Mapping[str, float],
    ent: Mapping[str, float],
    qcos: Mapping[str, float],
    *,
    per_signal: int = PER_SIGNAL,
    qcos_min: float = ASSOC_QCOS_MIN,
    max_n: int = ASSOC_MAX,
    ndigits: int = QCOS_NDIGITS,
) -> list[str]:
    """Union of the top-``per_signal`` candidates of each signal (ties: exact qcos
    desc, then id asc), kept when qcos >= ``qcos_min`` and cut to ``max_n`` by
    (qcos, id) descending. The gate and the final order compare qcos rounded to
    ``ndigits`` decimals: the precision the evaluated selection ran on
    (holdout_v1.py / gate_cv.py design()), so e.g. 0.39996 counts as 0.40."""
    union: list[str] = []
    for k in top_by_score(tag, qcos, per_signal) + top_by_score(ent, qcos, per_signal):
        if k not in union:
            union.append(k)
    rq = {k: round(qcos[k], ndigits) for k in union}
    keep = [k for k in union if rq[k] >= qcos_min]
    return sorted(keep, key=lambda k: (rq[k], k), reverse=True)[:max(max_n, 0)]


def query_cosines(query_vec, cand_vecs: Mapping[str, Sequence[float]]) -> dict[str, float]:
    """Cosine of the query embedding with each candidate embedding (row-normalised, float64).

    Candidates without a usable vector (missing, wrong dimension, zero norm) are left out.
    """
    if query_vec is None:
        return {}
    q = np.asarray(query_vec, dtype=np.float64).reshape(-1)
    qn = np.linalg.norm(q)
    if q.size == 0 or not np.isfinite(qn) or qn == 0:
        return {}
    rows: dict[str, np.ndarray] = {}
    for k, v in cand_vecs.items():
        if v is None:
            continue
        r = np.asarray(v, dtype=np.float64).reshape(-1)
        if r.shape == q.shape:
            rows[k] = r
    if not rows:
        return {}
    keys = list(rows)
    m = np.stack([rows[k] for k in keys])
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    ok = (norms[:, 0] > 0) & np.isfinite(norms[:, 0])
    cos = (m / np.where(norms > 0, norms, 1.0)) @ (q / qn)
    return {k: float(c) for k, c, good in zip(keys, cos.tolist(), ok.tolist()) if good}


# ---------------------------------------------------------------------------
# Per-user index cache (thread-safe, lazy, single-flight)
# ---------------------------------------------------------------------------
class IndexCache:
    """In-process per-user TTL cache of :class:`AssocIndex`.

    * fresh entry -> returned as is;
    * stale entry -> ``get``: the first caller rebuilds (others keep getting
      the stale entry instead of queueing behind the rebuild);
      ``get_or_refresh(background=True)``: returned at once while one daemon
      thread rebuilds it;
    * no entry -> callers wait for one shared build;
    * a failed build is not retried for ``retry_after_s`` (a stale entry, when
      present, keeps being served).
    Builders run in the caller's thread (wrap calls with ``asyncio.to_thread``).
    """

    def __init__(self, *, max_users: int = 8, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, AssocIndex]] = {}
        self._build_locks: dict[str, threading.Lock] = {}
        self._failed_at: dict[str, float] = {}
        self._refreshing: set[str] = set()
        self._max_users = max(int(max_users), 1)
        self._clock = clock

    def invalidate(self, user_id: str | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._entries.clear()
                self._failed_at.clear()
            else:
                self._entries.pop(user_id, None)
                self._failed_at.pop(user_id, None)

    def peek(self, user_id: str) -> AssocIndex | None:
        with self._lock:
            hit = self._entries.get(user_id)
        return hit[1] if hit else None

    def get_or_refresh(
        self,
        user_id: str,
        build: Callable[[], AssocIndex],
        *,
        ttl_s: float = INDEX_TTL_S,
        background: bool = True,
        retry_after_s: float = 30.0,
    ) -> AssocIndex | None:
        """``get`` that never makes a caller wait for a REbuild when ``background``."""
        if background:
            with self._lock:
                hit = self._entries.get(user_id)
                if hit is not None:
                    now = self._clock()
                    if now - hit[0] < ttl_s:
                        return hit[1]
                    failed = self._failed_at.get(user_id)
                    if user_id in self._refreshing or (
                        failed is not None and now - failed < retry_after_s
                    ):
                        return hit[1]
                    self._refreshing.add(user_id)
            if hit is not None:
                def _refresh() -> None:
                    try:
                        self.get(user_id, build, ttl_s=ttl_s, retry_after_s=retry_after_s)
                    except Exception:
                        pass  # failure recorded by get(); the stale entry stays
                    finally:
                        with self._lock:
                            self._refreshing.discard(user_id)

                try:
                    threading.Thread(target=_refresh, name="assoc-index-refresh", daemon=True).start()
                except Exception:
                    with self._lock:
                        self._refreshing.discard(user_id)
                return hit[1]
        return self.get(user_id, build, ttl_s=ttl_s, retry_after_s=retry_after_s)

    def get(
        self,
        user_id: str,
        build: Callable[[], AssocIndex],
        *,
        ttl_s: float = INDEX_TTL_S,
        retry_after_s: float = 30.0,
    ) -> AssocIndex | None:
        now = self._clock()
        with self._lock:
            hit = self._entries.get(user_id)
            if hit is not None and now - hit[0] < ttl_s:
                return hit[1]
            failed = self._failed_at.get(user_id)
            if failed is not None and now - failed < retry_after_s:
                return hit[1] if hit else None
            blk = self._build_locks.setdefault(user_id, threading.Lock())
        if hit is not None:
            if not blk.acquire(blocking=False):
                return hit[1]  # someone else is rebuilding: serve stale
        else:
            blk.acquire()
        try:
            with self._lock:
                cur = self._entries.get(user_id)
                if cur is not None and self._clock() - cur[0] < ttl_s:
                    return cur[1]
                failed = self._failed_at.get(user_id)
                if cur is None and failed is not None and self._clock() - failed < retry_after_s:
                    return None
            try:
                idx = build()
            except Exception:
                with self._lock:
                    self._failed_at[user_id] = self._clock()
                    stale = self._entries.get(user_id)
                if stale is not None:
                    return stale[1]
                raise
            with self._lock:
                self._entries[user_id] = (self._clock(), idx)
                self._failed_at.pop(user_id, None)
                while len(self._entries) > self._max_users:
                    oldest = min(self._entries, key=lambda u: self._entries[u][0])
                    self._entries.pop(oldest, None)
            return idx
        finally:
            blk.release()

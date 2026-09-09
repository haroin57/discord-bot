"""Two-stage dedup: cosine threshold + LLM judgment for gray zone."""

from __future__ import annotations

import logging

from .llm import complete_json, escape_braces
from .settings import settings

log = logging.getLogger(__name__)

# mem0's Chroma adapter returns `score = distance` (NOT similarity).
# ChromaDB default metric is L2; with OpenAI normalized embeddings this lands
# roughly in [0, 2]: 0 = identical text, ~1.0 = unrelated, ~1.5+ = opposite
# structure. So small distance ⇒ duplicate, large distance ⇒ novel.
#
# Observed examples (illustrative):
#   "Alice の音楽が好き" vs "Bob の音楽が好き"             → 0.817 (related, diff entity)
#   "Alice の音楽が好き" vs "Carol が好き"                → 0.991 (unrelated)
#   "Alice のライブに行った" vs "Bob の楽曲"              → 1.296 (unrelated)
DEDUP_HARD_DROP_DISTANCE = 0.15   # distance <= this → confirmed duplicate (drop)
DEDUP_GRAY_HIGH_DISTANCE = 0.35   # distance in (HARD, GRAY] → LLM judges
# distance > GRAY_HIGH → confirmed novel, keep

# Legacy aliases kept so any external import doesn't break. DO NOT use in
# new code — semantics are now distance-based.
DEDUP_HARD_DROP = DEDUP_HARD_DROP_DISTANCE
DEDUP_GRAY_LOW = DEDUP_GRAY_HIGH_DISTANCE

# Three-way relation verdict (replaces the old binary same/different judge).
# The old design collapsed "pure paraphrase" and "superset/complementary" into
# a single "same" verdict that dropped the NEW candidate — silently discarding
# the richer fact when the candidate added detail. Splitting them lets us drop
# only on true paraphrase and *synthesize* (止揚) on mergeable overlap.
_DEDUP_RELATION_PROMPT = """以下の2つの文の関係を判定してください。

既存の記憶: {existing}
新しい候補: {candidate}

判定 (3択、迷ったら "different"):
- "paraphrase": 同じ事実・同じ側面の単なる言い換え。情報の増減なし。
  - 「Rust が好き」vs「Rust を好んでいる」
  - 「dev ブランチに直接 push 禁止」vs「dev ブランチへの直接 push は禁止」
- "mergeable": 同じ事実・同じ側面だが、片方が日付/数値/固有名詞/限定詞/理由などの
  具体情報を追加している (上位互換または相互補完)。統合すべき。
  - 「大学からプログラミングを始めた」vs「大学一年から独学でプログラミングを始めた」
  - 「Denon AH-D5200 の購入を決定 (時期未定)」vs「Denon AH-D5200 を6/20購入決定、リスニング専用」
- "different": 別の側面・別の事実。両方を独立に保持すべき。
  - 「Rust を業務で使ってる」(行動) vs「Rust が好き」(嗜好)
  - 「Blade Runner を知ってるか質問した」(会話) vs「Blade Runner が好き」(嗜好)
  - 「取り返しのつかなさを描く作品が好き」(抽象) vs「Blade Runner が好き」(固有名詞)

誤統合より独立保持が安全。少しでも別の側面に見えたら "different"。

以下のJSONのみ返してください(説明不要):
{{"relation": "paraphrase" | "mergeable" | "different", "reason": "<30 文字以内>"}}"""

_DEDUP_MERGE_PROMPT = """次の2つの記憶は同じ事実を別の粒度・側面で述べています。
両方が持つ具体情報 (日付・数値・固有名詞・限定詞・理由) を一つも落とさずに
1文へ統合してください。

- 状態が変化している場合 (例: 未定→確定、旧値→新値) は新しい方の状態を採用し、
  古い状態は書かない。
- 冗長な重複表現は1回にまとめる。
- 主語・三人称/一人称・語尾は既存記憶の文体に合わせる。

既存の記憶: {existing}
新しい候補: {candidate}

以下のJSONのみ返してください(説明不要):
{{"merged": "<統合した1文>"}}"""

_VALID_RELATIONS = ("paraphrase", "mergeable", "different")


async def judge_fact_relation(existing_text: str, candidate_text: str) -> str:
    """Classify the relation between two facts.

    Returns one of ``paraphrase`` / ``mergeable`` / ``different``.
    On any error, returns ``different`` (fail-open: keep both, never drop
    or merge on a judge failure).
    """
    if max(len(existing_text), len(candidate_text)) > 4000:
        return "different"
    data = await complete_json(
        system=(
            "You classify the relation between two short fact statements "
            "as paraphrase / mergeable / different. Return JSON only."
        ),
        user_message=_DEDUP_RELATION_PROMPT.format(
            existing=escape_braces(existing_text),
            candidate=escape_braces(candidate_text),
        ),
        model=settings.MEM0_DEDUP_MODEL,
        max_tokens=settings.MEM0_DEDUP_MAX_TOKENS,
        caller="dedup.judge_fact_relation",
    )
    relation = (data or {}).get("relation") if isinstance(data, dict) else None
    return relation if relation in _VALID_RELATIONS else "different"


async def llm_judge_same_fact(existing_text: str, candidate_text: str) -> bool:
    """Back-compat shim: True when the two facts are not independent.

    Retained for callers/tests that want the old binary signal. Internally
    delegates to ``judge_fact_relation``; ``paraphrase``/``mergeable`` map to
    True (not independent), ``different`` maps to False. On error the relation
    judge fails open to ``different`` → returns False (keep the candidate).
    """
    return (await judge_fact_relation(existing_text, candidate_text)) != "different"


async def synthesize_facts(existing_text: str, candidate_text: str) -> str | None:
    """Aufhebung (止揚): merge two overlapping facts into one richer fact.

    Returns the synthesized fact text, or ``None`` on any error (fail-open:
    the caller keeps both facts rather than losing information).
    """
    if max(len(existing_text), len(candidate_text)) > 4000:
        return None
    data = await complete_json(
        system=(
            "You merge two overlapping fact statements into a single fact "
            "that preserves every concrete detail. Return JSON only."
        ),
        user_message=_DEDUP_MERGE_PROMPT.format(
            existing=escape_braces(existing_text),
            candidate=escape_braces(candidate_text),
        ),
        model=settings.MEM0_DEDUP_MERGE_MODEL,
        max_tokens=settings.MEM0_DEDUP_MAX_TOKENS,
        caller="dedup.synthesize_facts",
    )
    merged = (data or {}).get("merged") if isinstance(data, dict) else None
    if isinstance(merged, str) and merged.strip() and len(merged.strip()) <= 4000:
        return merged.strip()
    return None


_CLUSTER_CONSOLIDATE_PROMPT = """次の複数の記憶は同じ話題について別々のタイミングで保存されたものです。
重複・言い換え・進行状況の更新が混在しています。これらを統合してください。

規則:
- 同一事実の言い換え・重複は1つにまとめる
- 時系列で状態が進行している場合（例: 進捗、未定→確定）は最新状態を採用し、古い途中経過は捨てる
- 具体情報（日付・数値・固有名詞・ルート名など）は最新性を保ちつつ漏らさず保持
- **別々に保つべき独立した事実（別の側面）が混ざっていれば分けて出力してよい**（無理に1つにしない）
- 出力件数は元より必ず少なくする（最低でも1件減らせない場合は統合しない）

記憶:
{facts}

以下のJSONのみ返してください(説明不要):
{{"consolidated": ["統合後fact1", ...], "note": "<30字以内>"}}"""


async def synthesize_cluster(texts: list[str]) -> list[str] | None:
    """N-way consolidation (止揚) of a related-memory cluster.

    Returns the consolidated fact list (always SHORTER than the input on a
    successful consolidation), or ``None`` on any error / no-op (fail-open:
    the caller leaves the cluster untouched). Empirically validated: a 7-fact
    temporal cluster about one topic collapses to the single latest-state fact.

    Distinct aspects accidentally clustered together are split back out by the
    LLM (the prompt allows >1 output), so over-consolidation is bounded.
    """
    facts = [t for t in (texts or []) if isinstance(t, str) and t.strip()]
    if len(facts) < 2:
        return None
    if sum(map(len, facts)) > 32_000:
        return None  # Do not destroy details omitted from a bounded prompt.
    data = await complete_json(
        system=(
            "You consolidate a cluster of redundant memory facts into "
            "fewer facts, preserving every concrete detail and the latest "
            "state. Return JSON only."
        ),
        user_message=_CLUSTER_CONSOLIDATE_PROMPT.format(
            facts="\n".join(f"- {escape_braces(t)}" for t in facts),
        ),
        model=settings.MEM0_DEDUP_MERGE_MODEL,
        max_tokens=settings.MEM0_DEDUP_MAX_TOKENS,
        caller="dedup.synthesize_cluster",
    )
    consolidated = (data or {}).get("consolidated") if isinstance(data, dict) else None
    if not isinstance(consolidated, list):
        return None
    consolidated = [c.strip() for c in consolidated if isinstance(c, str) and c.strip()]
    # Must actually reduce the count; otherwise it's a no-op (skip).
    if not consolidated or len(consolidated) >= len(facts):
        return None
    return consolidated


async def partition_mergeable(texts: list[str]) -> list[list[int]]:
    """Complete-link groups: no explicitly different pair is ever merged."""
    compatible = {}
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i].strip(), texts[j].strip()
            compatible[i, j] = bool(a and a == b) or (
                await judge_fact_relation(a, b) in ("paraphrase", "mergeable"))
    groups = []
    for index in range(len(texts)):
        for group in groups:
            if all(compatible[min(index, member), max(index, member)] for member in group):
                group.append(index)
                break
        else:
            groups.append([index])
    return groups


def _log_dedup_decision(
    *, decision: str, cand: str, existing: str,
    distance: float, dry_run: bool,
    llm_same: bool | None = None,
) -> None:
    """One-line structured log so it can be grep'd later for calibration.

    Phase A-3 (mem0-recall-hybrid-hyde-kg): used by `journalctl | grep
    DEDUP_DECISION` to spot under/over-dropping over a 1-week window
    before deciding whether to retune DEDUP_HARD_DROP_DISTANCE /
    DEDUP_GRAY_HIGH_DISTANCE.
    """
    log.info(
        "[DEDUP_DECISION decision=%s dry_run=%s distance=%.3f llm_same=%s "
        "cand=%r existing=%r]",
        decision, dry_run, distance, llm_same,
        cand[:80], existing[:80],
    )


async def check_near_dup(
    candidate_text: str,
    candidate_category: str | None,
    near_results: list[dict],
    *,
    dry_run: bool = False,
    detect_conflict: bool = False,
    allow_merge: bool = True,
    candidate_metadata: dict | None = None,
) -> tuple[bool, str, str | None, str | None, str | None]:
    """Distance proposes candidates; identity/evidence decides destruction."""
    import math
    import re
    from .attribute_registry import can_supersede, fact_metadata, same_fact_domain, observation_time

    candidate_md = candidate_metadata or {}
    neighbors = []
    for row in near_results:
        if not isinstance(row, dict):
            continue
        score = row.get("score")
        text = row.get("memory") or row.get("text")
        if not isinstance(score, (int, float)) or not math.isfinite(score) or not isinstance(text, str):
            continue
        md = fact_metadata(row)
        if candidate_category and md.get("category") and candidate_category != md["category"]:
            continue
        if not same_fact_domain(md, candidate_md):
            continue
        neighbors.append((score, text, row.get("id"), md))
    # Prefer exact equality over a earlier approximate/mergeable neighbor.
    for score, text, fid, md in neighbors:
        if text.strip() == candidate_text.strip():
            return (not dry_run, "dry_run_would_exact" if dry_run else "exact", fid, None, None)
    for score, text, fid, md in sorted(neighbors, key=lambda row: row[0]):
        if score > DEDUP_GRAY_HIGH_DISTANCE:
            continue
        # Numbers/negation are cheap contradiction indicators, never proof
        # of paraphrase. LLM judgments cannot override these safety guards.
        numbers = lambda value: re.findall(r"[-+]?\d+(?:[.,]\d+)?", value)
        negation = lambda value: bool(re.search(r"ない|嫌い|やめ|なく|違う|not\b|never\b|no longer", value, re.I))
        changed = numbers(text) != numbers(candidate_text) or negation(text) != negation(candidate_text)
        old_value, new_value = md.get("value_json") or md.get("value"), candidate_md.get("value_json") or candidate_md.get("value")
        if old_value and new_value and old_value != new_value:
            changed = True
        relation = "different" if changed else await judge_fact_relation(text, candidate_text)
        if relation == "paraphrase":
            return (not dry_run, "dry_run_would_paraphrase" if dry_run else "paraphrase", fid, None, None)
        if relation == "mergeable" and allow_merge:
            # Merging typed or scoped facts must not synthesize a new text
            # while retaining an incompatible old structured value.
            if old_value != new_value:
                continue
            merged = await synthesize_facts(text, candidate_text)
            if merged and set(numbers(text) + numbers(candidate_text)) <= set(numbers(merged)):
                if dry_run:
                    return (False, "dry_run_would_merge", fid, None, None)
                return (False, "merged", fid, "merge", merged)
            continue
        if detect_conflict and can_supersede(md, candidate_md):
            from .conflict import classify_conflict
            cls = await classify_conflict(text, candidate_text, existing_metadata=md,
                                          candidate_metadata=candidate_md)
            if cls.get("retryable"):
                raise RuntimeError("conflict_classification_unavailable")
            kind = cls.get("conflict_type", "addition")
            if kind in ("update", "correction"):
                return (False, "conflict_" + kind, fid, None if dry_run else kind, None)
        elif detect_conflict and can_supersede(candidate_md, md):
            previous, incoming = observation_time(md), observation_time(candidate_md)
            if previous is not None and incoming is not None and incoming < previous:
                from .conflict import classify_conflict
                cls = await classify_conflict(candidate_text, text, existing_metadata=candidate_md,
                                              candidate_metadata=md)
                if cls.get("retryable"):
                    raise RuntimeError("conflict_classification_unavailable")
                kind = cls.get("conflict_type")
                if kind in ("update", "correction"):
                    return (False, "historical_" + kind, fid,
                            None if dry_run else "historical_" + kind, None)
    return (False, "novel", None, None, None)

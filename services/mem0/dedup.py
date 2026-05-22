"""Two-stage dedup: cosine threshold + LLM judgment for gray zone."""

import asyncio
import logging
import os

log = logging.getLogger(__name__)

# mem0's Chroma adapter returns `score = distance` (NOT similarity).
# ChromaDB default metric is L2; with OpenAI normalized embeddings this lands
# roughly in [0, 2]: 0 = identical text, ~1.0 = unrelated, ~1.5+ = opposite
# structure. So small distance ⇒ duplicate, large distance ⇒ novel.
#
# Observed examples (from 2026-05-14 logs):
#   "TAKU INOUEの音楽が好き" vs "Tomgggの音楽が好き"        → 0.817 (related, diff entity)
#   "TAKU INOUEの音楽が好き" vs "上伊那ぼたんが好き"        → 0.991 (unrelated)
#   "Midnight Grand Orch. ライブに行った" vs "ariiol楽曲"  → 1.296 (unrelated)
DEDUP_HARD_DROP_DISTANCE = 0.15   # distance <= this → confirmed duplicate (drop)
DEDUP_GRAY_HIGH_DISTANCE = 0.35   # distance in (HARD, GRAY] → LLM judges
# distance > GRAY_HIGH → confirmed novel, keep

# Legacy aliases kept so any external import doesn't break. DO NOT use in
# new code — semantics are now distance-based.
DEDUP_HARD_DROP = DEDUP_HARD_DROP_DISTANCE
DEDUP_GRAY_LOW = DEDUP_GRAY_HIGH_DISTANCE

_DEDUP_JUDGE_PROMPT = """以下の2つの文が「同一の事実」を述べているか判定してください。

既存の記憶: {existing}
新しい候補: {candidate}

判定基準 (厳格化、迷ったら false):
- **同じエンティティでも異なる aspect (側面) なら別**:
  - 「沙耶の唄を知ってるか質問した」(認知/会話) vs「沙耶の唄が好き」(嗜好) → **別**
  - 「Rust を業務で使ってる」(行動) vs「Rust が好き」(嗜好) → **別**
  - 「Namitape の my terminal をリピート」vs「Namitape の映像がすごい」→ **別**
- **抽象 fact と固有名詞 fact は別** (両方とも長期保存価値あり):
  - 「取り返しのつかなさを描く作品が好き」vs「沙耶の唄が好き」→ **別**
  - 「ノベルゲームが好き」vs「クラナドが好き」→ **別**
- **同一とみなす**のは「言い換え・同義表現」のみ:
  - 「Rust が好き」vs「Rust を好んでいる」→ 同一
  - 「dev ブランチに直接 push 禁止」vs「dev ブランチへの直接 push は禁止」→ 同一
- 上位互換 (新しい方が情報量多くて既存を含む) は「同一」扱い、ただし疑わしければ false

以下のJSONのみ返してください(説明不要):
{{"same_fact": true or false, "reason": "<30 文字以内>"}}"""


async def llm_judge_same_fact(existing_text: str, candidate_text: str) -> bool:
    """Ask Haiku whether two texts represent the same fact.

    Returns True if same (should drop candidate), False if different (keep).
    On any error, returns False (fail-open: keep the candidate).
    """
    try:
        from services.mem0.client import get_anthropic_client
        client = await get_anthropic_client()
        if client is None:
            return False
        safe_existing = existing_text[:300].replace("{", "{{").replace("}", "}}")
        safe_candidate = candidate_text[:300].replace("{", "{{").replace("}", "}}")
        from services.mem0.cli_compat import cli_messages_create, extract_text
        resp = await cli_messages_create(
            client,
            model=os.environ.get("MEM0_DEDUP_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=1500,
            user_message=_DEDUP_JUDGE_PROMPT.format(
                existing=safe_existing, candidate=safe_candidate,
            ),
            task_prompt=(
                "You judge whether two short fact statements describe the "
                "same fact. Return JSON only."
            ),
        )
        text = extract_text(resp).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        import json as _json
        return bool(_json.loads(text).get("same_fact", False))
    except Exception as exc:
        log.warning("dedup LLM judge failed (fail-open): %s", exc)
        return False


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
) -> tuple[bool, str, str | None]:
    """Two-stage dedup check against search results.

    Returns ``(is_dup, reason, existing_id)``.
    - is_dup=True  → candidate should be dropped; ``existing_id`` is the
      memory id of the existing fact it matched against (caller may use
      this to reinforce the existing fact's mention_count).
    - is_dup=False → candidate should be kept; ``existing_id`` is None.

    When ``dry_run=True`` the function still computes the full decision
    (including the LLM judge call so we measure its agreement rate) but
    always returns ``is_dup=False`` so nothing is actually dropped. The
    reason string is prefixed with ``dry_run_would_`` so logs can be
    grepped to identify drops that would have happened. ingest.py also
    has its own dry-run guard for defense-in-depth.
    """
    for r in near_results:
        score = r.get("score") or 0
        existing_text = r.get("memory", "")
        existing_id = r.get("id")

        # Category filter: skip dedup against different-category memories
        existing_category = (r.get("metadata") or {}).get("category")
        if candidate_category and existing_category and candidate_category != existing_category:
            continue

        # score is *distance* (small = similar). Sort-by-distance ascending
        # means the first iteration already has the smallest distance, but
        # we keep iterating just in case mem0 ever changes ordering.
        if score <= DEDUP_HARD_DROP_DISTANCE:
            _log_dedup_decision(
                decision="hard_drop", cand=candidate_text,
                existing=existing_text, distance=score, dry_run=dry_run,
            )
            log.info(
                "dedup HARD_DROP: cand='%s' | existing='%s' | distance=%.3f",
                candidate_text[:100], existing_text[:100], score,
            )
            if dry_run:
                return (False, f"dry_run_would_hard_drop(distance={score:.3f})", existing_id)
            return (True, f"hard_drop(distance={score:.3f})", existing_id)

        if score <= DEDUP_GRAY_HIGH_DISTANCE:
            is_same = await llm_judge_same_fact(existing_text, candidate_text)
            _log_dedup_decision(
                decision="gray_judged" if is_same else "gray_kept",
                cand=candidate_text, existing=existing_text,
                distance=score, dry_run=dry_run, llm_same=is_same,
            )
            log.info(
                "dedup GRAY: cand='%s' | existing='%s' | distance=%.3f | llm_same=%s",
                candidate_text[:100], existing_text[:100], score, is_same,
            )
            if is_same:
                if dry_run:
                    return (False, f"dry_run_would_llm_drop(distance={score:.3f})", existing_id)
                return (True, f"llm_judged_same(distance={score:.3f})", existing_id)

    return (False, "novel", None)

"""Two-stage dedup: cosine threshold + LLM judgment for gray zone."""

import asyncio
import logging
import os

log = logging.getLogger(__name__)

# Thresholds for 3-tier dedup decision
DEDUP_HARD_DROP = 0.95   # cosine >= this → confirmed duplicate, drop
DEDUP_GRAY_LOW = 0.80    # cosine in [gray_low, hard_drop) → ask LLM
# cosine < gray_low → confirmed novel, keep

_DEDUP_JUDGE_PROMPT = """以下の2つの文が「同一の事実」を述べているか判定してください。

既存の記憶: {existing}
新しい候補: {candidate}

判定基準:
- 同じエンティティに関する**異なる事実**は「別」(例: 「Xの映像がすごい」vs「Xのmy terminalをリピート」→ 別)
- 言い換え・同義表現は「同一」(例: 「Rustが好き」vs「Rustを好んでいる」→ 同一)
- 上位互換(新しい方が詳細)なら「同一」として扱う

以下のJSONのみ返してください(説明不要):
{{"same_fact": true or false}}"""


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
        resp = await client.messages.create(
            model=os.environ.get("MEM0_DEDUP_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=100,
            messages=[{"role": "user", "content": _DEDUP_JUDGE_PROMPT.format(
                existing=safe_existing, candidate=safe_candidate,
            )}],
        )
        text = resp.content[0].text.strip()
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


async def check_near_dup(
    candidate_text: str,
    candidate_category: str | None,
    near_results: list[dict],
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Two-stage dedup check against search results.

    Returns (is_dup, reason).
    - is_dup=True  → candidate should be dropped
    - is_dup=False → candidate should be kept
    """
    for r in near_results:
        score = r.get("score") or 0
        existing_text = r.get("memory", "")

        # Category filter: skip dedup against different-category memories
        existing_category = (r.get("metadata") or {}).get("category")
        if candidate_category and existing_category and candidate_category != existing_category:
            continue

        if score >= DEDUP_HARD_DROP:
            reason = f"hard_drop(score={score:.3f})"
            if dry_run:
                log.info("DRY-RUN dedup drop: %s | reason=%s | existing='%s'",
                         candidate_text[:80], reason, existing_text[:80])
            return (True, reason)

        if score >= DEDUP_GRAY_LOW:
            is_same = await llm_judge_same_fact(existing_text, candidate_text)
            if is_same:
                reason = f"llm_judged_same(score={score:.3f})"
                if dry_run:
                    log.info("DRY-RUN dedup drop: %s | reason=%s | existing='%s'",
                             candidate_text[:80], reason, existing_text[:80])
                return (True, reason)

    return (False, "novel")

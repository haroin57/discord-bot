"""Opus-judged retrieval quality on a hand-written golden scenario set.

Each scenario = {query, intent?}. We run search_invest_kb for each query,
hand the (query, top-5 chunks) pair to Opus, and ask for three scores in 0-2:

    relevance  — chunk addresses the query directly
    neutrality — chunk is free of sales pitch / position-talk / hype
    factual    — chunk states facts consistent with mainstream investment
                 knowledge (educator / academic / official disclosure)

We average per scenario and report top-failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

log = logging.getLogger(__name__)


JUDGE_MODEL = os.environ.get("INVEST_EVAL_CITATION_JUDGE_MODEL", "claude-opus-4-8")


_JUDGE_PROMPT = """個人投資家が中立な投資知識ベースで検索を行います。検索結果の品質を、
以下の観点で評価してください。

クエリ: \"\"\"{query}\"\"\"

検索結果（top-{n}）:
{snippets}

各観点を 0-2 で採点 (2=高品質, 1=ある程度OK, 0=不適):
- relevance: クエリの意図に対し直接的に答える内容か
- neutrality: 銘柄推奨やトレーディング戦略の煽り、ポジショントークが無いか
- factual: 投資の常識・教科書・公開資料と整合する事実が書かれているか

返答は生 JSON 1 個のみ。コードフェンス・解説は不要:
{{"relevance": 0/1/2, "neutrality": 0/1/2, "factual": 0/1/2, "comment": "簡潔なコメント"}}
"""


def _format_snippets(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        md = r.get("metadata") or {}
        src = md.get("source") or "?"
        title = md.get("title") or ""
        body = (r.get("memory") or "").strip().replace("\n", " ")[:300]
        lines.append(f"[{i}] source={src} title={title}\n  {body}")
    return "\n".join(lines)


async def _ask_judge(prompt: str) -> dict:
    from services.mem0.cli_compat import cli_messages_create, extract_text
    from services.mem0.client import get_anthropic_client
    client = await get_anthropic_client()
    if client is None:
        return {}
    resp = await cli_messages_create(
        client,
        model=JUDGE_MODEL,
        max_tokens=300,
        user_message=prompt,
        task_prompt="You score retrieval quality on three axes. JSON only.",
    )
    raw = extract_text(resp).strip()
    # strip optional code fence
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        # last-ditch: extract first {...} block
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


async def grade_citations(
    scenarios_path: str,
    *,
    limit: int | None = None,
    use_haiku: bool = True,
    concurrency: int = 10,
) -> dict:
    """Grade every scenario in the JSONL file. Returns aggregate + per-item.

    scenarios.jsonl line format: {"query": "...", "intent": "...optional..."}
    """
    scenarios: list[dict] = []
    with open(scenarios_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                scenarios.append(json.loads(line))
            except json.JSONDecodeError as exc:
                log.warning("scenarios: skip invalid line: %s", exc)
    if limit:
        scenarios = scenarios[:limit]

    sem = asyncio.Semaphore(concurrency)

    async def _one(sc):
        async with sem:
            from services.invest_kb import search_invest_kb
            q = sc["query"]
            try:
                results = await search_invest_kb(q, limit=5, use_haiku=use_haiku)
            except Exception as exc:
                return {"query": q, "error": str(exc)[:200], "scores": {}}
            if not results:
                return {"query": q, "scores": {}, "hits": 0, "comment": "(no hits)"}
            try:
                judged = await _ask_judge(
                    _JUDGE_PROMPT.format(
                        query=q, n=len(results), snippets=_format_snippets(results),
                    )
                )
            except Exception as exc:
                return {"query": q, "hits": len(results), "error": str(exc)[:200], "scores": {}}
            return {
                "query": q,
                "intent": sc.get("intent", ""),
                "hits": len(results),
                "top_id": results[0]["id"],
                "scores": {
                    "relevance": int(judged.get("relevance", 0) or 0),
                    "neutrality": int(judged.get("neutrality", 0) or 0),
                    "factual": int(judged.get("factual", 0) or 0),
                },
                "comment": judged.get("comment", "")[:200],
            }

    per_item = await asyncio.gather(*(_one(s) for s in scenarios))
    # Aggregate
    rels = [r["scores"].get("relevance", 0) for r in per_item if r.get("scores")]
    neus = [r["scores"].get("neutrality", 0) for r in per_item if r.get("scores")]
    facts = [r["scores"].get("factual", 0) for r in per_item if r.get("scores")]

    def _avg(xs):
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    return {
        "n": len(per_item),
        "average": {
            "relevance": _avg(rels),
            "neutrality": _avg(neus),
            "factual": _avg(facts),
        },
        "per_item": per_item,
    }

"""Semantic search: plain/multi/smart + LLM-based query rewrite & re-rank."""

import asyncio
import logging
import os

from services.mem0.client import (
    MEM0_RELEVANCE_THRESHOLD,
    _get_mem0,
    _refresh_mem0_client_token,
)
from services.oauth_manager import ensure_valid_token, get_access_token, init as oauth_init

log = logging.getLogger(__name__)


async def search_memories(query: str, user_id: str, limit: int = 8) -> list[dict]:
    """Semantic search for relevant memories with relevance filtering."""
    mem = _get_mem0()
    if not mem:
        return []
    try:
        await ensure_valid_token()  # refresh OAuth if needed
        _refresh_mem0_client_token()
        raw = await asyncio.to_thread(mem.search, query=query, user_id=user_id, limit=limit)
        results = raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        # Filter out low-relevance results
        filtered = [
            r for r in results
            if r.get("score") is None or r.get("score", 0) >= MEM0_RELEVANCE_THRESHOLD
        ]
        return filtered
    except Exception as exc:
        log.warning("Mem0 search error: %s", exc)
        return []


async def search_memories_multi(
    queries: list[str], user_id: str, limit: int = 8
) -> list[dict]:
    """Search with multiple queries and merge deduplicated results."""
    if not queries:
        return []
    tasks = [search_memories(q, user_id, limit=limit) for q in queries]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    seen_ids: set[str] = set()
    merged: list[dict] = []
    for result_set in all_results:
        if isinstance(result_set, Exception):
            continue
        for r in result_set:
            mid = r.get("id", r.get("memory", ""))
            if mid not in seen_ids:
                seen_ids.add(mid)
                merged.append(r)

    merged.sort(key=lambda x: x.get("score", 0), reverse=True)
    return merged[:limit]


# ---------------------------------------------------------------------------
# Search-time query rewriting + re-ranking (P3)
# ---------------------------------------------------------------------------
_REWRITE_PROMPT = """ユーザーの記憶検索クエリを、Mem0で類似ヒットしやすい正準キーワード3〜5個に変換してください。

ユーザークエリ: {query}

ヒント(代表的な正準ラベル):
- toeic-prep / grad-school-tokyo-science / engagement / meds
- ashitabo / kanae-bot / free-code / happy-stack / chatqa
- rust-async / quantum-arch / llm-architecture / formal-verification
- git-safety / vps-conoha / sops-age / claude-code-ops
- mermaid-prefs / arch-viz-prefs / clannad-routes / ariiol-songs

以下のJSONのみ返してください(説明や前置きなし):
{{"queries":["q1","q2","q3"]}}"""

_RERANK_PROMPT = """以下のMem0検索結果のうち、ユーザーの元クエリに本当に関連しているものを上位 {top_k} 件に絞ってください。

元クエリ: {query}

候補(番号: 内容):
{candidates}

関連度高い順に番号のみ返してください(JSON、説明なし):
{{"top":[3,1,5]}}"""


async def _rewrite_query(query: str) -> list[str]:
    """Expand a user query into canonical-keyword variants via Haiku."""
    try:
        from anthropic import AsyncAnthropic
        oauth_init()  # idempotent — load credentials + patch SDK
        await ensure_valid_token()
        token = get_access_token()
        if not token:
            return []
        client = AsyncAnthropic(auth_token=token)
        resp = await client.messages.create(
            model=os.environ.get("MEM0_REWRITE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=200,
            messages=[{"role": "user", "content": _REWRITE_PROMPT.format(query=query)}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        import json as _json
        data = _json.loads(text)
        out = data.get("queries", [])
        return [q for q in out if isinstance(q, str) and q.strip()][:5]
    except Exception as exc:
        log.warning("rewrite_query failed: %s", exc)
        return []


async def _rerank_memories(query: str, memories: list[dict], top_k: int = 5) -> list[dict]:
    """LLM-based re-ranking of Mem0 candidates by relevance to the original query."""
    if not memories:
        return []
    if len(memories) <= top_k:
        return memories
    try:
        from anthropic import AsyncAnthropic
        oauth_init()  # idempotent — load credentials + patch SDK
        await ensure_valid_token()
        token = get_access_token()
        if not token:
            return memories[:top_k]
        candidates = "\n".join(
            f"{i+1}: {m.get('memory','')[:120]}" for i, m in enumerate(memories)
        )
        client = AsyncAnthropic(auth_token=token)
        resp = await client.messages.create(
            model=os.environ.get("MEM0_RERANK_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=200,
            messages=[{"role": "user", "content": _RERANK_PROMPT.format(
                query=query, top_k=top_k, candidates=candidates,
            )}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        import json as _json
        idxs = _json.loads(text).get("top", [])
        picked = []
        for idx in idxs:
            try:
                k = int(idx) - 1
                if 0 <= k < len(memories):
                    picked.append(memories[k])
            except (TypeError, ValueError):
                continue
        # fallback fill with score-order if reranker returned fewer
        if len(picked) < top_k:
            seen_ids = {id(p) for p in picked}
            for m in memories:
                if id(m) not in seen_ids:
                    picked.append(m)
                    if len(picked) >= top_k:
                        break
        return picked[:top_k]
    except Exception as exc:
        log.warning("rerank_memories failed: %s", exc)
        return memories[:top_k]


async def search_memories_smart(
    query: str, user_id: str, limit: int = 5,
    rewrite: bool = True, rerank: bool = True,
) -> list[dict]:
    """Query-rewriting + re-ranking enhanced search.

    Behavior:
      1. Rewrite the user query into ~3-5 canonical-keyword variants (Haiku).
      2. Run search_memories_multi over [original + variants] in parallel.
      3. Re-rank the merged top-20 by LLM-judged relevance and return top-`limit`.

    Both `rewrite` and `rerank` can be disabled to fall back to plain search.
    """
    queries = [query]
    if rewrite:
        variants = await _rewrite_query(query)
        queries = [query] + [v for v in variants if v != query]
    candidates = await search_memories_multi(queries, user_id, limit=20)
    if rerank:
        return await _rerank_memories(query, candidates, top_k=limit)
    return candidates[:limit]

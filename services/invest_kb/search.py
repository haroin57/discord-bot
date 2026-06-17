"""invest_kb search: Haiku rewrite + HyDE + dual-vector ANN + Haiku rerank.

Returned dicts mirror Mem0's search result shape so bot.py can pass them to
`format_memories_for_prompt` with light wrapping for source attribution.
"""
from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger(__name__)


# Curated gate keywords. Tuned to fire on:
#   - 投資 / 株 / 資産運用 vocabulary
#   - 制度名 (NISA, iDeCo, つみたて)
#   - 評価指標 (PER, PBR, ROE, 配当)
#   - 商品 (ETF, 投資信託, 債券, インデックス)
#   - 専門単語 (有報, 決算, 自己資本, バリュエーション)
# Conservative — false negatives are fine (user can /context to peek), false
# positives blow Haiku budget on chitchat.
_GATE_KEYWORDS_JA = (
    # core finance vocabulary
    "投資", "株式", "株価", "資産", "運用", "NISA", "iDeCo", "つみたて",
    "ETF", "投資信託", "債券", "金利", "配当", "利回り", "インデックス",
    "アクティブ", "PER", "PBR", "ROE", "ROA", "EPS", "決算", "有報",
    "貸借", "損益", "自己資本", "バリュエーション", "ポートフォリオ",
    "リバランス", "アセットアロケーション", "リスク資産", "ベンチマーク",
    "TOPIX", "日経平均", "S&P", "ドルコスト", "リーマン", "暴落", "バブル",
    "経常利益", "営業利益", "純資産", "時価総額", "ファンダメンタル",
    # academic / theoretical concepts added in Patch 2b after the E2E run
    # showed gated=False on queries like "CAPM の前提と限界" — the KB now
    # covers these well so a missed gate is pure lost recall.
    "CAPM", "資本資産価格モデル", "EMH", "効率的市場", "効率市場",
    "現代ポートフォリオ理論", "MPT", "シャープレシオ", "シャープ・レシオ", "シャープ比",
    "ファクター投資", "ファクター", "リスクプレミアム", "リスクフリー",
    "信用リスク", "金利リスク", "為替リスク", "ボラティリティ", "標準偏差",
    "ベータ", "アルファ", "市場ポートフォリオ", "分散投資", "シングルファクター",
    "マルチファクター", "ロー・ボラティリティ", "クオリティ", "モメンタム",
    "債券価格", "デュレーション", "信用格付け", "格付", "国債",
    "確定拠出年金", "確定給付年金", "所得控除", "譲渡所得", "配当所得",
    "特定口座", "一般口座", "源泉徴収", "総合課税", "申告分離",
    "決算短信", "MD&A", "経営者の所信表明", "セグメント情報", "有価証券報告書",
)
_GATE_KEYWORDS_EN = (
    " invest", "stock", "equity", "bond", "yield", "dividend", "nisa",
    "ideco", "etf", "valuation", "portfolio", "fund", "benchmark",
)


def is_investment_query(text: str) -> bool:
    """Cheap keyword gate to decide whether to spend Haiku budget on a query."""
    if not text:
        return False
    s = text.lower()
    for kw in _GATE_KEYWORDS_JA:
        if kw in text:  # JP keywords are case-irrelevant
            return True
    for kw in _GATE_KEYWORDS_EN:
        if kw in s:
            return True
    return False


def _row_to_result(row: dict, score: float) -> dict:
    """Convert a LanceDB row to the Mem0-shaped result dict.

    ``score`` here is the raw cosine similarity (1.0 - cosine distance), kept
    separately on the result as ``cosine_score`` so the downstream RRF merge
    can overwrite ``score`` without losing the raw signal the noise gate
    (Patch 3) needs.
    """
    return {
        "id": row.get("id") or "",
        "memory": row.get("summary") or row.get("text") or "",
        "score": float(score),
        "cosine_score": float(score),
        "metadata": {
            "source": row.get("source") or "",
            "source_id": row.get("source_id") or "",
            "source_url": row.get("source_url") or "",
            "title": row.get("title") or "",
            "lang": row.get("lang") or "",
            "license": row.get("license") or "",
            "category": row.get("category") or "",
            "text": row.get("text") or "",
        },
    }


async def _embed_one(text: str) -> list[float]:
    from services.invest_kb.embed import embed_batch
    out = await embed_batch([text])
    return out[0] if out else []


async def _vector_search(tbl, vec: list[float], col: str, limit: int) -> list[dict]:
    """Run a single ANN search on the given vector column."""
    if not vec:
        return []
    # LanceDB returns _distance ascending (lower = closer for cosine).
    df = (
        tbl.search(vec, vector_column_name=col)
        .metric("cosine")
        .limit(limit)
        .to_pandas()
    )
    rows = []
    for _, r in df.iterrows():
        d = float(r.get("_distance", 1.0))
        sim = 1.0 - d  # cosine sim
        rows.append(_row_to_result(r.to_dict(), sim))
    return rows


def _rrf_merge(*lists: list[dict], k: int = 60, limit: int = 20) -> list[dict]:
    """Reciprocal rank fusion across multiple ranked lists by id.

    Each merged item keeps the max raw cosine_score it received in any
    list — Patch 3's noise gate filters on this so an embedding-fakehit that
    only ranks high by coincidence is still cut.
    """
    score: dict[str, float] = {}
    items: dict[str, dict] = {}
    best_cosine: dict[str, float] = {}
    for lst in lists:
        for rank, item in enumerate(lst):
            cid = item["id"]
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank + 1)
            cs = float(item.get("cosine_score", item.get("score", 0.0)))
            if cs > best_cosine.get(cid, -1.0):
                best_cosine[cid] = cs
            if cid not in items:
                items[cid] = item
    merged = sorted(items.values(), key=lambda x: score[x["id"]], reverse=True)
    for m in merged:
        m["score"] = score[m["id"]]
        m["cosine_score"] = best_cosine.get(m["id"], 0.0)
    return merged[:limit]


async def search_invest_kb(
    query: str,
    *,
    limit: int = 5,
    use_haiku: bool = True,
) -> list[dict]:
    """End-to-end search.

    Pipeline:
      1. Haiku rewrite -> 3 variants
      2. Haiku HyDE -> 1 hypothetical
      3. embed [original, *rewrites, hypothetical] concurrently
      4. ANN each query against vector_body and vector_summary
      5. RRF merge into top-20
      6. Haiku rerank -> top-`limit`
    """
    from services.invest_kb.client import get_table
    q = (query or "").strip()
    if not q:
        return []
    tbl = get_table()
    if tbl.count_rows() == 0:
        return []

    queries: list[str] = [q]
    hyde_text = ""
    if use_haiku:
        from services.invest_kb import haiku
        rewrite_task = asyncio.create_task(haiku.rewrite_query(q))
        hyde_task = asyncio.create_task(haiku.hyde(q))
        rewrites, hyde_text = await asyncio.gather(rewrite_task, hyde_task)
        queries.extend(rewrites)
        if hyde_text:
            queries.append(hyde_text)

    # Embed all variants in one batched call.
    from services.invest_kb.embed import embed_batch
    vecs = await embed_batch(queries)

    # ANN each variant against both vectors, in parallel.
    per_variant_lim = max(8, limit * 4)
    tasks = []
    for v in vecs:
        tasks.append(_vector_search(tbl, v, "vector_body", per_variant_lim))
        tasks.append(_vector_search(tbl, v, "vector_summary", per_variant_lim))
    hits = await asyncio.gather(*tasks)
    merged = _rrf_merge(*hits, limit=max(20, limit * 5))

    # Patch 3 (2026-06-17) — noise gate. Embedding cosine fakes a ~0.30-0.36
    # match on completely unrelated text via shared kanji/loanword frequency,
    # which dragged citation relevance scores down to 0 on concepts the KB
    # doesn't actually cover. Drop everything below the gate so we just return
    # zero hits instead of injecting noise.
    from config import INVEST_KB_MIN_SCORE
    if INVEST_KB_MIN_SCORE > 0:
        merged = [m for m in merged if m.get("cosine_score", 0.0) >= INVEST_KB_MIN_SCORE]

    # Final rerank
    if use_haiku and len(merged) > limit:
        from services.invest_kb import haiku
        return await haiku.rerank(q, merged, top_k=limit)
    return merged[:limit]


def format_for_prompt(results: list[dict], *, max_chars: int = 1500) -> str:
    """Render results as the <shared_investment_knowledge> body."""
    if not results:
        return ""
    lines: list[str] = []
    used = 0
    for r in results:
        md = r.get("metadata") or {}
        tag = f"{md.get('source','?')}:{md.get('source_id','?')}"
        body = (r.get("memory") or "").strip().replace("\n", " ")
        title = md.get("title")
        prefix = f"- [{tag}] "
        if title:
            prefix += f"{title} — "
        line = prefix + body
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)

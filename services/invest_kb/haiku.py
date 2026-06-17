"""Haiku-backed preprocessing + search refinement for invest_kb.

All Haiku calls reuse Mem0's shared Anthropic client wiring so they go through
the same OAuth refresh + retry path as the rest of the bot. Each function
returns a sensible fallback on any failure — the upstream chunker/search must
not abort on an LLM hiccup.

Functions:
    summarize_chunk    — 1 Haiku call returning {summary, keywords, entities}
    rewrite_query      — 3-5 paraphrase variants of a search query
    hyde               — single hypothetical answer for HyDE-style vector search
    rerank             — pick top_k from N candidates by Haiku judgment
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

HAIKU_MODEL = os.environ.get("INVEST_KB_HAIKU_MODEL", "claude-haiku-4-5-20251001")
SUMMARY_MAX_CHARS = 90


_SUMMARIZE_PROMPT = """次のテキストを投資知識ベースに登録するために前処理してください。

テキスト:
\"\"\"
{text}
\"\"\"

以下のJSONで返答してください。コードフェンスや解説は不要、生JSONのみ:
{{
  "summary": "本文の要点を90字以内の自然な日本語1文にまとめる。固有名詞・数値・年は維持。",
  "keywords": ["最大10個までの内容語キーワード（投資テーマ、概念、固有名詞）"],
  "entities": ["最大10個までの固有名詞（企業名、人名、制度名）"]
}}
"""


_REWRITE_PROMPT = """次の検索クエリを、同じ意図の異なる表現に3つだけ書き換えてください。
原クエリ: \"\"\"{query}\"\"\"

JSONで返答 (生JSONのみ):
{{"queries": ["書き換え1", "書き換え2", "書き換え3"]}}
"""


_HYDE_PROMPT = """次の質問に対し、もし投資解説書/教科書/有報があれば書かれているであろう
事実文を1つだけ仮想的に書いてください。短く具体的に。

質問: \"\"\"{query}\"\"\"

JSONで返答 (生JSONのみ):
{{"hypothetical": "事実文をここに"}}
"""


_RERANK_PROMPT = """ユーザーの質問に対し、候補ドキュメントの中で**最も関連性が高く、
中立性も高い** {top_k} 件を選んでください。トレーディング戦略の煽り、銘柄推奨、
ポジショントークが含まれるものは順位を下げてください。

質問: \"\"\"{query}\"\"\"

候補:
{candidates}

JSONで返答 (生JSONのみ):
{{"top_ids": ["id1", "id2", ...]}}  // 上位{top_k}件、降順
"""


def _safe(text: str, limit: int = 500) -> str:
    return (text or "")[:limit].replace("{", "{{").replace("}", "}}")


def _strip_codefence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        # ```json ... ```
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    return text.strip()


async def _call_haiku(prompt: str, *, max_tokens: int, task_prompt: str) -> str:
    from services.mem0.cli_compat import cli_messages_create, extract_text
    from services.mem0.client import get_anthropic_client
    client = await get_anthropic_client()
    if client is None:
        return ""
    resp = await cli_messages_create(
        client,
        model=HAIKU_MODEL,
        max_tokens=max_tokens,
        user_message=prompt,
        task_prompt=task_prompt,
    )
    return extract_text(resp).strip()


async def summarize_chunk(text: str, lang: str = "ja") -> dict:
    """Generate {summary, keywords, entities} for a chunk. Always returns a
    dict with those three keys; falls back to text-prefix on any failure."""
    fallback = {
        "summary": (text or "")[:SUMMARY_MAX_CHARS],
        "keywords": [],
        "entities": [],
    }
    if not (text or "").strip():
        return fallback
    try:
        raw = await _call_haiku(
            _SUMMARIZE_PROMPT.format(text=_safe(text, 2000)),
            max_tokens=400,
            task_prompt="You extract a 90-char summary plus keywords/entities. JSON only.",
        )
        data = json.loads(_strip_codefence(raw))
        summary = str(data.get("summary") or "").strip()[:SUMMARY_MAX_CHARS]
        kws = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
        ents = [str(e).strip() for e in (data.get("entities") or []) if str(e).strip()]
        return {
            "summary": summary or fallback["summary"],
            "keywords": kws[:10],
            "entities": ents[:10],
        }
    except Exception as exc:
        log.warning("summarize_chunk failed (non-fatal): %s", exc)
        return fallback


async def rewrite_query(query: str) -> list[str]:
    """Return 1-3 paraphrases. Original query is NOT included; the caller
    is responsible for sending the original too."""
    q = (query or "").strip()
    if len(q) < 5:
        return []
    try:
        raw = await _call_haiku(
            _REWRITE_PROMPT.format(query=_safe(q)),
            max_tokens=300,
            task_prompt="You rewrite the user query into 3 equivalents. JSON only.",
        )
        data = json.loads(_strip_codefence(raw))
        out = [str(x).strip() for x in (data.get("queries") or []) if str(x).strip()]
        # De-dup + drop the original verbatim
        seen: set[str] = {q}
        uniq: list[str] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq[:3]
    except Exception as exc:
        log.warning("rewrite_query failed (non-fatal): %s", exc)
        return []


async def hyde(query: str) -> str:
    q = (query or "").strip()
    if len(q) < 5:
        return ""
    try:
        raw = await _call_haiku(
            _HYDE_PROMPT.format(query=_safe(q)),
            max_tokens=250,
            task_prompt="You produce one hypothetical sentence. JSON only.",
        )
        data = json.loads(_strip_codefence(raw))
        return str(data.get("hypothetical") or "").strip()
    except Exception as exc:
        log.warning("hyde failed (non-fatal): %s", exc)
        return ""


async def rerank(query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
    """candidates = [{id, text or summary, source, ...}] (any extra keys OK).
    Returns candidates re-ordered to top_k length. Falls back to input order
    on any failure."""
    if not candidates:
        return []
    if len(candidates) <= top_k:
        return candidates[:top_k]
    try:
        # Build the prompt with id + short context per candidate.
        lines = []
        idx: dict[str, dict] = {}
        for c in candidates:
            cid = str(c.get("id") or "")
            idx[cid] = c
            snippet = (
                (c.get("summary") or c.get("memory") or c.get("text") or "")[:240]
                .replace("\n", " ")
            )
            src = c.get("source") or (c.get("metadata") or {}).get("source") or ""
            lines.append(f"- id={cid} source={src}: {snippet}")
        raw = await _call_haiku(
            _RERANK_PROMPT.format(
                query=_safe(query, 800),
                candidates="\n".join(lines)[:8000],
                top_k=top_k,
            ),
            max_tokens=400,
            task_prompt="You pick the top-N most relevant + neutral candidates. JSON only.",
        )
        data = json.loads(_strip_codefence(raw))
        ordered: list[dict] = []
        seen_ids: set[str] = set()
        for cid in (data.get("top_ids") or []):
            cid_s = str(cid)
            if cid_s in idx and cid_s not in seen_ids:
                ordered.append(idx[cid_s])
                seen_ids.add(cid_s)
            if len(ordered) >= top_k:
                break
        if not ordered:
            return candidates[:top_k]
        # Pad from input order if Haiku returned fewer than top_k.
        if len(ordered) < top_k:
            for c in candidates:
                cid_s = str(c.get("id") or "")
                if cid_s not in seen_ids:
                    ordered.append(c)
                    seen_ids.add(cid_s)
                    if len(ordered) >= top_k:
                        break
        return ordered[:top_k]
    except Exception as exc:
        log.warning("rerank failed, falling back to input order: %s", exc)
        return candidates[:top_k]

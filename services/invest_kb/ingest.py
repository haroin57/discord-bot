"""invest_kb document ingest.

`upsert_documents(rows)` takes a list of "row dicts" (one per source-level
document) and:
  1. chunks each row's text,
  2. runs Haiku summarize on each chunk,
  3. embeds body + summary in batch,
  4. atomically replaces each document in LanceDB.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

log = logging.getLogger(__name__)


async def _summarize_all(chunks_text: list[str], lang: str, concurrency: int = 10):
    """Bounded-concurrency summarize for a chunk list."""
    from services.invest_kb import haiku

    sem = asyncio.Semaphore(concurrency)

    async def _one(t):
        async with sem:
            return await haiku.summarize_chunk(t, lang=lang)

    return await asyncio.gather(*[_one(t) for t in chunks_text])


async def upsert_documents(
    rows: Iterable[dict],
    *,
    target_chars: int = 800,
    max_chars: int = 1200,
    overlap: int = 120,
    dry_run: bool = False,
    summarize_concurrency: int = 10,
) -> dict:
    """Upsert a batch of source-level documents.

    Each row must include at least:
        text, source, source_id, lang
    Optional: title, source_url, license, category, published_at

    Returns a dict with counters:
        {"rows": N, "documents": D, "chunks": M, "skipped": K, "took_s": T}
    """
    from services.invest_kb.chunker import chunk_text
    from services.invest_kb.client import make_chunk_id, replace_document

    rows = list(rows)
    t_start = time.monotonic()

    # Step 1: chunk every row.
    chunk_records: list[dict] = []
    document_keys: set[tuple[str, str]] = set()
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        source = str(row.get("source") or "")
        source_id = str(row.get("source_id") or "")
        if not source or not source_id:
            raise ValueError("documents require non-empty source and source_id")
        document_key = (source, source_id)
        if document_key in document_keys:
            raise ValueError(
                f"duplicate document in one batch: {source}:{source_id}",
            )
        document_keys.add(document_key)
        lang = row.get("lang") or "ja"
        chunks = chunk_text(
            text,
            target=target_chars,
            max_size=max_chars,
            overlap=overlap,
            lang=lang,
        )
        for idx, c in enumerate(chunks):
            chunk_records.append(
                {
                    "row": row,
                    "chunk_text": c.text,
                    "chunk_idx": idx,
                    "lang": lang,
                }
            )
    if not chunk_records:
        return {"rows": len(rows), "chunks": 0, "skipped": 0, "took_s": 0.0}

    if dry_run:
        return {
            "rows": len(rows),
            "chunks": len(chunk_records),
            "skipped": 0,
            "took_s": round(time.monotonic() - t_start, 2),
            "dry_run": True,
        }

    # Step 2: Haiku summarize each chunk (bounded concurrency).
    log.info("summarize: %d chunks", len(chunk_records))
    summaries = await _summarize_all(
        [c["chunk_text"] for c in chunk_records],
        lang=chunk_records[0]["lang"],
        concurrency=summarize_concurrency,
    )
    if len(summaries) != len(chunk_records):
        raise RuntimeError("summarizer returned an incomplete document batch")

    # Step 3: Embed body + summary in one big batched call (preserving order).
    from services.invest_kb.embed import embed_batch

    bodies = [c["chunk_text"] for c in chunk_records]
    summary_texts = [s.get("summary") or "" for s in summaries]
    log.info("embed: %d body + %d summary", len(bodies), len(summary_texts))
    vec_body, vec_summary = await asyncio.gather(
        embed_batch(bodies),
        embed_batch(summary_texts),
    )
    if len(vec_body) != len(chunk_records) or len(vec_summary) != len(chunk_records):
        raise RuntimeError("embedder returned an incomplete document batch")

    # Step 4: Build rows and atomically replace each source document. A failed
    # merge leaves that document's previous LanceDB version visible.
    out_rows: list[dict] = []
    for rec, summ, vb, vs in zip(chunk_records, summaries, vec_body, vec_summary):
        row = rec["row"]
        source = str(row.get("source") or "")
        source_id = str(row.get("source_id") or "")
        out_rows.append(
            {
                "id": make_chunk_id(source, source_id, rec["chunk_idx"]),
                "text": rec["chunk_text"],
                "summary": (summ.get("summary") or "")[:90],
                "keywords": ",".join(summ.get("keywords") or [])[:512],
                "entities": ",".join(summ.get("entities") or [])[:512],
                "vector_body": vb,
                "vector_summary": vs,
                "source": source,
                "source_id": source_id,
                "source_url": str(row.get("source_url") or ""),
                "title": str(row.get("title") or ""),
                "lang": rec["lang"],
                "license": str(row.get("license") or ""),
                "published_at": int(row.get("published_at") or 0),
                "category": str(row.get("category") or ""),
            }
        )

    rows_by_document: dict[tuple[str, str], list[dict]] = {}
    for row in out_rows:
        key = (row["source"], row["source_id"])
        rows_by_document.setdefault(key, []).append(row)
    for document_rows in rows_by_document.values():
        await replace_document(document_rows)

    return {
        "rows": len(rows),
        "documents": len(rows_by_document),
        "chunks": len(out_rows),
        "skipped": 0,
        "took_s": round(time.monotonic() - t_start, 2),
        "dry_run": False,
    }

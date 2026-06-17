"""Invest knowledge base — side store for long-form neutral investment knowledge.

Why separate from Mem0:
    Mem0 is designed for "fact extraction + 120-char compression + conversation
    seeded ingest". Dumping long documentary text (EDINET narrative, blog
    chapters, CFA notebook markdown) through Mem0's extract pipeline either
    discards information or trips its dedup. invest_kb keeps the raw chunks +
    Haiku-generated summary/keywords/entities in LanceDB so the bot can recall
    them in parallel with Mem0 without contaminating fact distance space.

Public API:
    add_documents       — upsert idempotently via id PK
    search_invest_kb    — Haiku rewrite + HyDE + dual-vector + Haiku rerank
    is_investment_query — gate to avoid Haiku spend on small talk
"""

from services.invest_kb.ingest import upsert_documents as add_documents
from services.invest_kb.search import is_investment_query, search_invest_kb

__all__ = [
    "add_documents",
    "is_investment_query",
    "search_invest_kb",
]

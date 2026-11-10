"""Invest knowledge base — side store for long-form neutral investment knowledge.

Why separate from Mem0:
    Mem0 is designed for "fact extraction + 120-char compression + conversation
    seeded ingest". Dumping long documentary text (EDINET narrative, blog
    chapters, CFA notebook markdown) through Mem0's extract pipeline either
    discards information or trips its dedup. invest_kb keeps the raw chunks +
    Haiku-generated summary/keywords/entities in LanceDB so the bot can recall
    them in parallel with Mem0 without contaminating fact distance space.

Public API:
    add_documents       — prepare and atomically replace source documents
    replace_document    — atomic prepared-chunk replacement contract
    search_invest_kb    — Haiku rewrite + HyDE + dual-vector + Haiku rerank
    is_investment_query — gate to avoid Haiku spend on small talk
"""

from importlib import import_module

_EXPORTS = {
    "add_documents": ("ingest", "upsert_documents"),
    "replace_document": ("client", "replace_document"),
    "is_investment_query": ("search", "is_investment_query"),
    "search_invest_kb": ("search", "search_invest_kb"),
}


def __getattr__(name: str):
    """Load only the requested capability, preserving public function identity."""
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    return getattr(import_module(f"{__name__}.{module}"), attribute)

__all__ = [
    "add_documents",
    "is_investment_query",
    "replace_document",
    "search_invest_kb",
]

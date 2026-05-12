"""Mem0 memory service package — semantic long-term memory for the Discord bot.

Public API re-exports for `from services.mem0 import ...`. Internal modules:
  client   — singleton + OAuth + config (also low-level get_all/delete)
  ingest   — add_memories (P2 gate + legacy fallback)
  search   — search/search_multi/search_smart + rewrite/rerank
  extract  — extract_facts_for_self + _classify_for_metadata
  format   — format_memories_for_prompt
"""

from services.mem0.client import (
    MEM0_RELEVANCE_THRESHOLD,
    delete_memory,
    get_all_memories,
)
from services.mem0.extract import extract_facts_for_self
from services.mem0.format import format_memories_for_prompt
from services.mem0.ingest import add_memories
from services.mem0.search import (
    search_memories,
    search_memories_multi,
    search_memories_smart,
)

__all__ = [
    "MEM0_RELEVANCE_THRESHOLD",
    "add_memories",
    "delete_memory",
    "extract_facts_for_self",
    "format_memories_for_prompt",
    "get_all_memories",
    "search_memories",
    "search_memories_multi",
    "search_memories_smart",
]

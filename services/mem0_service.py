"""LEGACY re-export module. New code should `from services.mem0 import ...` directly.
Kept for backward compat (weekly_mem0_compact.py uses private symbols).
"""

from services.mem0 import *  # noqa: F401,F403
from services.mem0.client import (  # noqa: F401
    _get_mem0,
    _refresh_mem0_client_token,
)
from services.mem0.extract import _classify_for_metadata  # noqa: F401
from services.mem0.search import _rerank_memories, _rewrite_query  # noqa: F401

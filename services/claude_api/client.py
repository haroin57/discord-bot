"""Shared client-side constants used by both __init__ and entry.

Lifted to its own module so submodules (entry.py, tool_loop.py) can import
these constants without going through the package __init__ which itself
imports those submodules — a circular import.
"""

import uuid

CC_VERSION = "2.1.92"
CC_ENTRYPOINT = "sdk-py"

# Betas for Opus (free-code: getAllModelBetas)
MODEL_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "context-1m-2025-08-07",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
]

_session_id = str(uuid.uuid4())

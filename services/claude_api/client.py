"""Shared client-side constants used by both __init__ and entry.

Lifted to its own module so submodules (entry.py, tool_loop.py) can import
these constants without going through the package __init__ which itself
imports those submodules — a circular import.
"""

import uuid

CC_VERSION = "2.1.92"
CC_ENTRYPOINT = "cli"  # Matches the real claude-cli User-Agent suffix.

# User-Agent the real CLI uses. Captured via mitmproxy on /v1/messages from
# claude-cli 2.1.92. The desktop bundle ships "Claude-Desktop" but the CLI
# is what we want to mimic since we have the same surface shape (no GUI).
USER_AGENT = f"claude-cli/{CC_VERSION} (external, {CC_ENTRYPOINT})"

# Minimal subscriber-tier beta set. Per spacewander_lzx's "Anthropic Beta
# Header 全景" writeup, several beta headers REQUIRE matching body fields:
#   interleaved-thinking-2025-05-14 → body needs "thinking": {"type": ...}
#   effort-2025-11-24               → body needs "output_config": {effort: }
#   context-management-2025-06-27   → body needs "context_management": {...}
#   advanced-tool-use-2025-11-20    → tool schemas need defer_loading
#   redact-thinking-2026-02-12      → requires thinking enabled
# Advertising them without the matching body fields triggers 429 / 400.
# We currently send a plain messages payload, so we keep only the four
# subscriber-essential betas and add the others back as we add the body
# fields they require.
MODEL_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "context-1m-2025-08-07",
    "prompt-caching-scope-2026-01-05",
]

_session_id = str(uuid.uuid4())

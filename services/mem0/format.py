"""Format retrieved Mem0 memories as a prompt-injection block."""

import re

# Markers an attacker could have stored in a fact (via the ingestion path)
# that would close the <retrieved_memories> fence or impersonate the trusted
# context block when echoed back into the prompt. Strip them defensively at
# format time as second-order injection defense.
_FACT_TEXT_BLOCKLIST = re.compile(
    r"</retrieved_memories>"
    r"|<retrieved_memories[^>]*>"
    r"|\[End of Mem0 memories\]"
    r"|\[Mem0 [^\]\n]*\]"
    r"|\[Trusted context[^\]\n]*\]",
    re.IGNORECASE,
)


def _scrub_fact_text(text: str) -> str:
    """Drop markers that could close the memories fence or impersonate trust headers."""
    if not text:
        return text
    return _FACT_TEXT_BLOCKLIST.sub("[redacted-marker]", text)


def format_memories_for_prompt(memories: list[dict]) -> str:
    """Format retrieved memories as a prompt-injection block."""
    if not memories:
        return ""

    lines = ["[Mem0 — この人についての長期記憶]"]
    for m in memories:
        text = m.get("memory", m.get("text", ""))
        if text:
            lines.append(f"- {_scrub_fact_text(text)}")
    lines.append("[End of Mem0 memories]")
    return "\n".join(lines)

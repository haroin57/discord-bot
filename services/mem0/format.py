"""Format retrieved Mem0 memories as a prompt-injection block."""

import re

# Markers an attacker could have stored in a fact (via the ingestion path)
# that would close the <retrieved_memories> fence or impersonate any trusted
# prompt-scaffold block when echoed back into the prompt. Strip them
# defensively at format time as second-order injection defense.
#
# This pattern set MIRRORS `_SENTINEL_RE` in bot.py / services/prompt_builder.py
# so a fact echoed via format_memories_for_prompt cannot inject a scaffold
# marker that the outer sanitize layer used to catch. The outer
# `_sanitize_untrusted_text(mem0_block)` call was removed for hot-path
# performance, so this blocklist is now the single source of scrubbing.
_FACT_TEXT_BLOCKLIST = re.compile(
    r"\[(?:Message from|End of|Mem0|Attached images|Current message|Thread:|"
    r"Channel recent|Referenced message|Replied-to message|End of thread|"
    r"End of channel|End of Mem0|Trusted context|untrusted_user_input)"
    r"[^\]\n]*\]"
    r"|</?retrieved_memories[^>]*>"
    r"|</?untrusted_user_input[^>]*>",
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

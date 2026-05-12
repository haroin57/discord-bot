"""Shared prompt-injection sentinel regex + sanitize helper.

`bot.py` and `services/prompt_builder.py` previously held identical copies of
`_SENTINEL_RE` and `_POWER_PHRASE_RE`. Every time we added a marker (Phase 4
of the security overhaul added `</retrieved_memories>` / `[Trusted context]`,
for example) the regex had to be updated in two places. This module is the
single source of truth; both call sites now import from here.

The patterns themselves are unchanged from the post-Phase-4 state — this is
a refactor, not a behavior change.
"""

import re

__all__ = [
    "SENTINEL_RE",
    "POWER_PHRASE_RE",
    "sanitize_untrusted_text",
]


# Sentinel labels that the trusted prompt scaffold uses. If a user input
# contains them verbatim, strip them so the attacker cannot close a tag and
# inject a new, fake authority header.
SENTINEL_RE = re.compile(
    r"\[(?:Message from|End of|Mem0|Attached images|Current message|Thread:|"
    r"Channel recent|Referenced message|Replied-to message|End of thread|"
    r"End of channel|End of Mem0|Trusted context|untrusted_user_input)[^\]\n]*\]"
    r"|</?retrieved_memories[^>]*>"
    r"|</?untrusted_user_input[^>]*>",
    re.IGNORECASE,
)

# Power-grab phrases — patterns that try to impersonate trusted instructions.
POWER_PHRASE_RE = re.compile(
    r"(adminユーザー|bypassPermissions|bypass[_\- ]permissions|全機能利用可能|"
    r"all[_\- ]permissions|system[_\- ]prompt|root[_\- ]access|ignore (?:the )?(?:previous|above)|"
    r"disregard (?:the )?(?:previous|above)|</untrusted_user_input>)",
    re.IGNORECASE,
)


def sanitize_untrusted_text(text: str) -> str:
    """Remove prompt-scaffold sentinel labels and obvious power-grab phrases.

    Applied to every piece of untrusted text before it is concatenated into
    the Claude prompt. Idempotent — running it twice on already-sanitized
    text returns the same result, so layered defenses can call it freely.
    """
    if not text:
        return text
    text = SENTINEL_RE.sub("[redacted-marker]", text)
    text = POWER_PHRASE_RE.sub("[redacted]", text)
    return text

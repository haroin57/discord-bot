"""Shared prompt-injection sentinel regex + sanitize helper.

`bot.py` and `services/prompt_builder.py` previously held identical copies of
`_SENTINEL_RE` and `_POWER_PHRASE_RE`. Every time we added a marker (Phase 4
of the security overhaul added `</retrieved_memories>` / `[Trusted context]`,
for example) the regex had to be updated in two places. This module is the
single source of truth; both call sites now import from here.

The patterns themselves are unchanged from the post-Phase-4 state — this is
a refactor, not a behavior change.

Unicode bypass mitigation: callers that perform regex matching on untrusted
text MUST call `normalize_for_sentinel(text)` first. This folds Unicode
lookalikes (NFKC) and strips zero-width characters that are otherwise
invisible to the patterns below. `apply_sentinel_filters` bundles both steps
for convenience — prefer it over calling the regexes directly.
"""

import re
import unicodedata

__all__ = [
    "SENTINEL_RE",
    "POWER_PHRASE_RE",
    "sanitize_untrusted_text",
    "normalize_for_sentinel",
    "apply_sentinel_filters",
]

# Zero-width / invisible characters that can split keywords to evade matching.
_ZERO_WIDTH_CHARS = (
    "​",  # zero-width space
    "‌",  # zero-width non-joiner
    "‍",  # zero-width joiner
    "⁠",  # word joiner
    "﻿",  # zero-width no-break space (BOM)
    "­",  # soft hyphen
    "͏",  # combining grapheme joiner
)


def normalize_for_sentinel(text: str) -> str:
    """Normalize untrusted text before sentinel/power-phrase matching.

    Applies NFKC Unicode normalization (folds Latin-Extended lookalikes and
    other compatibility equivalents to their canonical ASCII/base forms) and
    strips zero-width invisible characters that can split keywords.

    Callers: invoke this on every untrusted string before applying
    SENTINEL_RE or POWER_PHRASE_RE.
    """
    text = unicodedata.normalize("NFKC", text)
    for ch in _ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")
    return text


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

    Note: does NOT normalize Unicode. Callers that may receive adversarial
    input should use `apply_sentinel_filters` instead.
    """
    if not text:
        return text
    text = SENTINEL_RE.sub("[redacted-marker]", text)
    text = POWER_PHRASE_RE.sub("[redacted]", text)
    return text


def apply_sentinel_filters(text: str) -> str:
    """Normalize then sanitize untrusted text in one call.

    Equivalent to::

        sanitize_untrusted_text(normalize_for_sentinel(text))

    PromptBuilder.sanitize should switch to this function to gain Unicode
    bypass protection without any other changes.
    """
    if not text:
        return text
    text = normalize_for_sentinel(text)
    text = SENTINEL_RE.sub("[redacted-marker]", text)
    text = POWER_PHRASE_RE.sub("[redacted]", text)
    return text

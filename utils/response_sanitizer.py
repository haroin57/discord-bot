"""Strip spurious inline-code wrapping around numeric/currency/range tokens.

Claude occasionally wraps figures like `746,404（約1.1億円）` or `300K-` in
single backticks, which Discord renders as a white-boxed inline code block.
This sanitizer removes the backticks ONLY when the content is purely
numeric-ish (digit + currency/range punctuation), and never on real code
tokens (identifiers, paths, expressions).

Math (`$..$`, `$$..$$`) and fenced code blocks (` ``` `) are untouched by
construction — the former uses different delimiters, the latter is pre-masked
via utils.markdown_parser.mask_code_blocks().
"""

import re

from utils.markdown_parser import mask_code_blocks

__all__ = ["strip_numeric_inline_code"]

# Single-backtick inline code: not preceded/followed by another `, no newlines,
# bounded length so we don't accidentally strip large blocks.
_INLINE_CODE_RE = re.compile(r"(?<!`)`(?!`)([^`\n]{1,120})(?<!`)`(?!`)")

# Must contain at least one digit — pure English words like `equity` or `if`
# never qualify for stripping.
_HAS_DIGIT_RE = re.compile(r"\d")

# Numeric helper words that may legitimately appear alongside figures and
# should NOT trigger code-signal protection (e.g. "400K + equity").
_NUMERIC_HELPER_WORDS = (
    "equity", "bonus", "stock", "options", "RSU", "RSUs",
)
_HELPER_ALT = "|".join(re.escape(w) + r"\b" for w in _NUMERIC_HELPER_WORDS)

# Any of these signals indicates real code: leave the backticks alone.
#   - Bracket/operator characters (parens, brackets, braces, slashes, etc.)
#   - Dot-notation (os.path) — only when dot is adjacent to a LETTER, so that
#     decimal numbers like 1.05 or 0.05 are not protected.
#   - Snake_case identifiers — underscore adjacent to a letter.
#   - Arrow operators (->, =>)
#   - Runs of 4+ ASCII letters (English words / identifiers), except a small
#     whitelist of numeric helper words.
_CODE_SIGNAL_RE = re.compile(
    r"[(){}\[\]<>;:=&|/\\]"
    r"|\.[a-zA-Z]|[a-zA-Z]\."
    r"|_[a-zA-Z]|[a-zA-Z]_"
    r"|->|=>"
    rf"|\b(?!(?:{_HELPER_ALT}))[a-zA-Z]{{4,}}\b"
)


def strip_numeric_inline_code(text: str) -> str:
    """Strip backticks around inline-code tokens that are only numeric/currency.

    Args:
        text: Raw model response text.

    Returns:
        Text with backticks removed around numeric-only inline-code spans.
        Real code tokens (`range(10)`, `if x:`, `/tmp/foo.png`) are preserved.
    """
    if not text or "`" not in text:
        return text

    # Mask fenced code blocks so their inner backticks don't get matched here.
    # mask_code_blocks preserves length so offsets line up with `text`.
    masked = mask_code_blocks(text)

    pieces: list[str] = []
    last = 0
    for m in _INLINE_CODE_RE.finditer(masked):
        s, e = m.span()
        pieces.append(text[last:s])
        original = text[s:e]
        inner = original[1:-1].strip()
        if (
            inner
            and _HAS_DIGIT_RE.search(inner)
            and not _CODE_SIGNAL_RE.search(inner)
        ):
            pieces.append(original[1:-1])
        else:
            pieces.append(original)
        last = e
    pieces.append(text[last:])
    return "".join(pieces)

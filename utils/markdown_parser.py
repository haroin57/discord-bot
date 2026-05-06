"""Markdown table and math detection and segment splitting.

Splits Claude responses into text, table, and math segments so that tables
and math expressions can be rendered as images while text is sent as regular
Discord messages.
"""

import re
from dataclasses import dataclass

__all__ = [
    "TextSegment", "TableSegment", "MathSegment", "Segment",
    "parse_segments", "has_tables", "has_math",
]


@dataclass
class TextSegment:
    content: str


@dataclass
class TableSegment:
    markdown: str
    caption: str = ""


@dataclass
class MathSegment:
    """A LaTeX math expression to be rendered as an image."""
    latex: str
    display_mode: bool = True  # True for $$..$$, False for $..$ (block vs inline)


Segment = TextSegment | TableSegment | MathSegment

# ---------------------------------------------------------------------------
# Regex: matches Markdown tables (header + separator + data rows).
# Excludes tables inside fenced code blocks by pre-stripping them.
# ---------------------------------------------------------------------------
TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)"  # header row
    r"(?:^[ \t]*\|[-:| ]+\|[ \t]*\n)"  # separator row
    r"(?:^[ \t]*\|.+\|[ \t]*\n?)+)",  # data rows (1+)
    re.MULTILINE,
)

# Fenced code block (``` ... ```)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)

# ---------------------------------------------------------------------------
# Math detection: $$ ... $$ (display) and $ ... $ (inline)
# ---------------------------------------------------------------------------
# Display math: $$ on its own line (or surrounded by whitespace)
DISPLAY_MATH_RE = re.compile(
    r"(\$\$\n[\s\S]*?\n\$\$"       # $$ on own lines
    r"|\$\$[^\$]+?\$\$)",           # $$ inline $$
    re.MULTILINE,
)

# Inline math: single $ ... $ (not preceded/followed by $)
INLINE_MATH_RE = re.compile(
    r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)",
)

# Combined: find all math (display first, then inline)
_ALL_MATH_RE = re.compile(
    r"(\$\$[\s\S]*?\$\$"           # display math $$...$$
    r"|(?<!\$)\$(?!\$)(?!\s)(.+?)(?<!\s)(?<!\$)\$(?!\$))",  # inline $...$
    re.MULTILINE,
)


def _mask_code_blocks(text: str) -> str:
    """Replace code blocks with placeholder of same length to preserve offsets."""
    def _replacer(m: re.Match) -> str:
        # Replace with spaces/newlines to keep line structure
        return re.sub(r"[^\n]", " ", m.group(0))
    return _CODE_BLOCK_RE.sub(_replacer, text)


def has_tables(text: str) -> bool:
    """Lightweight check: does the text contain at least one Markdown table?"""
    masked = _mask_code_blocks(text)
    return bool(TABLE_RE.search(masked))


def has_math(text: str) -> bool:
    """Lightweight check: does the text contain LaTeX math expressions?"""
    masked = _mask_code_blocks(text)
    return bool(_ALL_MATH_RE.search(masked))


def _split_math_in_text(text: str) -> list[Segment]:
    """Split a text fragment into Text and Math segments."""
    masked = _mask_code_blocks(text)
    segments: list[Segment] = []
    last_end = 0

    for m in _ALL_MATH_RE.finditer(masked):
        start, end = m.start(), m.end()
        before = text[last_end:start]
        if before.strip():
            segments.append(TextSegment(content=before))

        raw_match = text[start:end]
        if raw_match.startswith("$$"):
            # Display math — strip $$ delimiters
            latex = raw_match[2:-2].strip()
            segments.append(MathSegment(latex=latex, display_mode=True))
        else:
            # Inline math — strip $ delimiters
            latex = raw_match[1:-1].strip()
            segments.append(MathSegment(latex=latex, display_mode=False))
        last_end = end

    remaining = text[last_end:]
    if remaining.strip():
        segments.append(TextSegment(content=remaining))

    return segments


def parse_segments(text: str) -> list[Segment]:
    """Split response text into Text, Table, and Math segments.

    Tables inside fenced code blocks are ignored. If a heading (### ...)
    immediately precedes a table, it is absorbed as the table caption.
    Math expressions ($..$ and $$..$$) are also extracted.
    """
    masked = _mask_code_blocks(text)
    segments: list[Segment] = []
    last_end = 0

    for m in TABLE_RE.finditer(masked):
        start, end = m.start(), m.end()
        before = text[last_end:start]

        # Extract caption from heading just before the table
        caption = ""
        heading_match = re.search(r"(^#{1,4}\s+.+)$\s*\Z", before, re.MULTILINE)
        if heading_match:
            caption = heading_match.group(1).lstrip("#").strip()
            before = before[: heading_match.start()].rstrip()

        if before.strip():
            # Check for math in the text before the table
            segments.extend(_split_math_in_text(before))

        # Use the *original* text for the table content (not masked)
        segments.append(TableSegment(markdown=text[start:end], caption=caption))
        last_end = end

    remaining = text[last_end:]
    if remaining.strip():
        # Check for math in remaining text
        segments.extend(_split_math_in_text(remaining))

    return segments

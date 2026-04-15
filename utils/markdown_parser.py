"""Markdown table detection and segment splitting.

Splits Claude responses into text and table segments so that tables
can be rendered as images while text is sent as regular Discord messages.
"""

import re
from dataclasses import dataclass

__all__ = ["TextSegment", "TableSegment", "Segment", "parse_segments", "has_tables"]


@dataclass
class TextSegment:
    content: str


@dataclass
class TableSegment:
    markdown: str
    caption: str = ""


Segment = TextSegment | TableSegment

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


def parse_segments(text: str) -> list[Segment]:
    """Split response text into Text and Table segments.

    Tables inside fenced code blocks are ignored. If a heading (### ...)
    immediately precedes a table, it is absorbed as the table caption.
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
            segments.append(TextSegment(content=before))

        # Use the *original* text for the table content (not masked)
        segments.append(TableSegment(markdown=text[start:end], caption=caption))
        last_end = end

    remaining = text[last_end:]
    if remaining.strip():
        segments.append(TextSegment(content=remaining))

    return segments

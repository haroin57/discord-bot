"""Markdown table and math detection and segment splitting.

Splits Claude responses into text, table, and math segments so that tables
and math expressions can be rendered as images while text is sent as regular
Discord messages.
"""

import re
from dataclasses import dataclass

__all__ = [
    "TextSegment", "TableSegment", "MathSegment", "Segment",
    "parse_segments", "has_tables", "has_math", "mask_code_blocks",
    "split_settled", "first_block_span",
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

# Inline math: single $ ... $ (not preceded/followed by $). Requires a LaTeX
# marker in the body so currency ($50) isn't matched — see _ALL_MATH_RE.
INLINE_MATH_RE = re.compile(
    r"(?<!\$)\$(?!\$)(.*?[\\^_{}=].*?)(?<!\$)\$(?!\$)",
)

# Combined: find all math (display first, then inline).
# Inline $...$ REQUIRES a LaTeX marker (= \ ^ _ { }) in its body so that plain
# currency like "$50、送料$10" / "$5から$10" / "$100-$200" is NOT mistaken for
# math (the bug: two $ separated by non-space JP/punct/hyphen matched as math
# and got rendered as a broken KaTeX image). Real math ($E=mc^2$, $\frac{a}{b}$,
# $x^2$) always carries one of those markers. Trade-off: a marker-less single
# variable like $x$ is no longer treated as inline math — acceptable, since the
# currency false-positive is far more common and damaging than that miss.
_ALL_MATH_RE = re.compile(
    r"(\$\$[\s\S]*?\$\$"           # display math $$...$$
    r"|(?<!\$)\$(?!\$)(?!\s)(.*?[\\^_{}=].*?)(?<!\s)(?<!\$)\$(?!\$))",  # inline $...$ (LaTeX-marker required)
    re.MULTILINE,
)


def mask_code_blocks(text: str) -> str:
    """Replace code blocks with placeholder of same length to preserve offsets.

    Public so callers can compute the masked form once and pass it to multiple
    detectors instead of re-scanning the same text via has_tables/has_math/
    parse_segments separately.
    """
    def _replacer(m: re.Match) -> str:
        # Replace with spaces/newlines to keep line structure
        return re.sub(r"[^\n]", " ", m.group(0))
    return _CODE_BLOCK_RE.sub(_replacer, text)


# Backwards-compat alias (some internal callers still use the underscore name).
_mask_code_blocks = mask_code_blocks


def has_tables(text: str, *, _masked: str | None = None) -> bool:
    """Lightweight check: does the text contain at least one Markdown table?

    If the caller has already computed the masked form via mask_code_blocks(),
    pass it via the keyword-only `_masked` arg to skip a redundant scan.
    """
    masked = _masked if _masked is not None else mask_code_blocks(text)
    return bool(TABLE_RE.search(masked))


def has_math(text: str, *, _masked: str | None = None) -> bool:
    """Lightweight check: does the text contain LaTeX math expressions?

    See has_tables() for the optional `_masked` arg.
    """
    masked = _masked if _masked is not None else mask_code_blocks(text)
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


# ---------------------------------------------------------------------------
# Incremental streaming split (B): separate a still-growing response into a
# settled prefix (all renderable blocks closed → safe to finalize via
# parse_segments + image render) and a pending tail (may still grow).
# ---------------------------------------------------------------------------

# A line that begins a Markdown table row. Anchored at the start only (not the
# closing |) so a row still being typed ("| 1 | 2" with no closing pipe yet) is
# also recognised — otherwise the tail would fall through to TABLE_RE, which can
# match an earlier complete-looking prefix ("| 1 |") and emit a TRUNCATED table.
_TABLE_LINE_RE = re.compile(r"^[ \t]*\|")
# Any code fence delimiter (opening or closing).
_FENCE_RE = re.compile(r"```")


def _last_unclosed_fence_start(text: str) -> int | None:
    """Offset of an unclosed ``` fence (odd number of fences), else None.

    Everything from this offset on is a code block still being typed: it is
    plain streamable text (rendered as a code block, never an image) and must
    not be scanned for `$`/`$$`/table delimiters.
    """
    starts = [m.start() for m in _FENCE_RE.finditer(text)]
    return starts[-1] if len(starts) % 2 == 1 else None


def _trailing_table_start(masked: str) -> int | None:
    """Start offset of a table-ish run that touches the tail, else None.

    A Markdown table is only *settled* once a non-table line (blank or prose)
    follows it — until then more rows may arrive. This also catches a table
    being typed before TABLE_RE can match (header / header+separator only).
    Returns None when the last non-blank line is not a table row.
    """
    lines = masked.splitlines(keepends=True)
    start: int | None = None
    k = len(lines) - 1
    while k >= 0:
        stripped = lines[k].rstrip("\n")
        if stripped.strip() == "":
            break  # a blank line terminates the table → it's settled
        if _TABLE_LINE_RE.match(stripped):
            start = sum(len(x) for x in lines[:k])
            k -= 1
            continue
        break  # non-table, non-blank line → no trailing table
    return start


def split_settled(text: str) -> tuple[str, str, bool]:
    """Split a streaming-in-progress response into (settled, pending, open).

    ``settled``  — leading prefix in which every renderable block ($$..$$,
                   $..$, Markdown table) is fully closed. Safe to finalize:
                   parse_segments() + render block images, immutable thereafter.
    ``pending``  — trailing region that may still grow.
    ``open``     — True when ``pending`` begins with an *unclosed renderable
                   block* (open $$, open inline $, or a table still
                   accumulating). The caller then shows a "rendering…"
                   placeholder and holds — never the raw block. False when
                   ``pending`` is plain prose / an open code fence, which the
                   caller streams live as an editable draft.

    Invariant: settled + pending == text.
    """
    if not text:
        return "", "", False

    # An unclosed trailing code fence: code being typed. It's plain streamable
    # text (a code block, not an image), so it belongs to the live prose tail —
    # and we must not scan inside it for math/table delimiters.
    fence_open = _last_unclosed_fence_start(text)
    scan_limit = fence_open if fence_open is not None else len(text)

    head = text[:scan_limit]
    masked = mask_code_blocks(head)  # neutralize CLOSED code fences

    # --- 1. Find an OPEN renderable block (the held boundary, if any). ---
    open_start: int | None = None

    # Unclosed display math: odd number of $$ delimiters.
    dd = [m.start() for m in re.finditer(r"\$\$", masked)]
    if len(dd) % 2 == 1:
        open_start = dd[-1]

    # Unclosed inline math: odd number of single $ after neutralizing $$ pairs.
    # Two guards so we don't hold on a literal $ (currency):
    #   - newline after the open $ → literal $ (e.g. a price on its own line)
    #   - the tail after the open $ has no LaTeX marker (= \ ^ _ { }) → it's
    #     currency like "$50", not math being typed (mirrors _ALL_MATH_RE).
    masked_no_dd = re.sub(r"\$\$", "  ", masked)
    singles = [m.start() for m in re.finditer(r"\$", masked_no_dd)]
    if len(singles) % 2 == 1:
        pos = singles[-1]
        tail = text[pos:scan_limit]
        if "\n" not in tail and re.search(r"[\\^_{}=]", tail):
            open_start = pos if open_start is None else min(open_start, pos)

    # A table touching the tail: still growing / awaiting a terminating line.
    tbl_start = _trailing_table_start(masked)
    if tbl_start is not None:
        open_start = tbl_start if open_start is None else min(open_start, tbl_start)

    if open_start is not None:
        # Prose before the open block is terminated by it → settled. The open
        # block (and anything after) is held: never shown raw.
        return text[:open_start], text[open_start:], True

    # --- 2. No open block: the trailing prose run (everything after the last
    #        CLOSED renderable block) is still growing → live-draft pending. ---
    last_end = 0
    for m in TABLE_RE.finditer(masked):
        last_end = max(last_end, m.end())
    for m in _ALL_MATH_RE.finditer(masked):
        last_end = max(last_end, m.end())
    # text[last_end:] is plain prose (plus any open code fence region after
    # scan_limit, which is also live text) — stream it as an editable draft.
    return text[:last_end], text[last_end:], False


def first_block_span(text: str) -> tuple[int, int, "Segment"] | None:
    """Leftmost CLOSED renderable block (table or math) in ``text``.

    Returns ``(start, end, segment)`` with raw character offsets into ``text``
    (``text[start:end]`` is the raw block), or ``None`` when there is no block.
    Intended for an already-settled prefix (see split_settled), where every
    block is closed. A table absorbs an immediately-preceding heading line as
    its caption — the returned ``start`` then includes that heading so the
    caller doesn't leave it dangling as prose. Used by the incremental streamer
    to render each block image in place as soon as it closes.
    """
    masked = mask_code_blocks(text)
    best: tuple[int, int, str] | None = None  # (start, end, kind)

    tm = TABLE_RE.search(masked)
    if tm is not None:
        best = (tm.start(), tm.end(), "table")
    mm = _ALL_MATH_RE.search(masked)
    if mm is not None and (best is None or mm.start() < best[0]):
        best = (mm.start(), mm.end(), "math")

    if best is None:
        return None

    start, end, kind = best
    raw = text[start:end]

    if kind == "table":
        caption = ""
        before = text[:start]
        heading_match = re.search(r"(^#{1,4}\s+.+)$\s*\Z", before, re.MULTILINE)
        if heading_match:
            caption = heading_match.group(1).lstrip("#").strip()
            start = heading_match.start()  # fold the heading into the block span
        return start, end, TableSegment(markdown=raw, caption=caption)

    # math
    if raw.startswith("$$"):
        return start, end, MathSegment(latex=raw[2:-2].strip(), display_mode=True)
    return start, end, MathSegment(latex=raw[1:-1].strip(), display_mode=False)

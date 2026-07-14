"""Markdown table and math detection and segment splitting.

Splits Claude responses into text, table, and math segments so that tables
and math expressions can be rendered as images while text is sent as regular
Discord messages.
"""

import re
from dataclasses import dataclass

from utils.math_presentation import PresentationKind, classify_math

__all__ = [
    "TextSegment", "TableSegment", "MathSegment", "Segment",
    "parse_segments", "has_tables", "has_math", "mask_code_blocks",
    "format_math_for_discord", "split_settled", "first_block_span",
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
# Math detection: $$ ... $$ / \[ ... \] (display) and
# $ ... $ / \( ... \) (inline)
# ---------------------------------------------------------------------------
# Display math: $$ on its own line (or surrounded by whitespace)
DISPLAY_MATH_RE = re.compile(
    r"(\$\$\n[\s\S]*?\n\$\$"       # $$ on own lines
    r"|\$\$[^\$]+?\$\$"            # $$ inline $$
    r"|\\\[[\s\S]*?\\\])",        # \[ ... \]
    re.MULTILINE,
)

# Inline math candidate: single $ ... $ on one line. Semantic filtering below
# separates math from currency without allowing one pair to consume another.
INLINE_MATH_RE = re.compile(
    r"(?<!\$)\$(?!\$)([^\n$]+?)(?<!\$)\$(?!\$)"
    r"|\\\(([\s\S]*?)\\\)",
)

# Combined candidate regex. Inline bodies explicitly exclude `$`, so adjacent
# expressions such as `$n$ ... $A^n$` can never be merged into one expression.
_MATH_CANDIDATE_RE = re.compile(
    r"(\$\$[\s\S]*?\$\$"           # display math $$...$$
    r"|\\\[[\s\S]*?\\\]"         # display math \[...\]
    r"|\\\([\s\S]*?\\\)"         # inline math \(...\)
    r"|(?<!\$)\$(?!\$)(?!\s)([^\n$]+?)(?<!\s)(?<!\$)\$(?!\$))",
    re.MULTILINE,
)

_INLINE_TEX_MARKER_RE = re.compile(r"[\\^_{}=]")
_INLINE_SIMPLE_MATH_RE = re.compile(
    r"^(?=.*[A-Za-z\u0370-\u03ff])[A-Za-z0-9\u0370-\u03ff+*/(),.\-\s]+$"
)


def _looks_like_inline_math(body: str) -> bool:
    """Accept TeX syntax and simple symbolic expressions, but not prices."""
    if not body or body != body.strip():
        return False
    if _INLINE_TEX_MARKER_RE.search(body):
        return True
    return bool(_INLINE_SIMPLE_MATH_RE.fullmatch(body))


def _iter_math_matches(text: str):
    for match in _MATH_CANDIDATE_RE.finditer(text):
        raw = match.group(0)
        if raw.startswith(("$$", r"\[", r"\(")) or _looks_like_inline_math(
            raw[1:-1]
        ):
            yield match


def _math_segment(raw: str) -> MathSegment:
    """Convert one closed raw delimiter block into a MathSegment."""
    if raw.startswith("$$") or raw.startswith(r"\["):
        return MathSegment(latex=raw[2:-2].strip(), display_mode=True)
    if raw.startswith(r"\("):
        return MathSegment(latex=raw[2:-2].strip(), display_mode=False)
    return MathSegment(latex=raw[1:-1].strip(), display_mode=False)


def _iter_renderable_math_matches(text: str, *, adaptive_math: bool):
    for match in _iter_math_matches(text):
        if not adaptive_math:
            yield match
            continue
        segment = _math_segment(match.group(0))
        if classify_math(segment).kind is PresentationKind.IMAGE:
            yield match


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


def format_math_for_discord(text: str) -> str:
    """Replace safely representable closed TeX with inline Discord text.

    Complex expressions stay byte-for-byte unchanged so the image block
    pipeline can still identify them from raw offsets. Fenced code is masked
    during detection and therefore never rewritten.
    """
    masked = mask_code_blocks(text)
    parts: list[str] = []
    last_end = 0
    for match in _iter_math_matches(masked):
        presentation = classify_math(_math_segment(text[match.start():match.end()]))
        if presentation.kind is not PresentationKind.TEXT:
            continue
        parts.append(text[last_end:match.start()])
        parts.append(presentation.text)
        last_end = match.end()
    if not parts:
        return text
    parts.append(text[last_end:])
    return "".join(parts)


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
    return next(_iter_math_matches(masked), None) is not None


def _split_math_in_text(text: str) -> list[Segment]:
    """Split a text fragment into Text and Math segments."""
    masked = _mask_code_blocks(text)
    segments: list[Segment] = []
    last_end = 0

    for m in _iter_math_matches(masked):
        start, end = m.start(), m.end()
        before = text[last_end:start]
        if before.strip():
            segments.append(TextSegment(content=before))

        raw_match = text[start:end]
        segments.append(_math_segment(raw_match))
        last_end = end

    remaining = text[last_end:]
    if remaining.strip():
        segments.append(TextSegment(content=remaining))

    return segments


def parse_segments(text: str) -> list[Segment]:
    r"""Split response text into Text, Table, and Math segments.

    Tables inside fenced code blocks are ignored. If a heading (### ...)
    immediately precedes a table, it is absorbed as the table caption.
    Math expressions ($..$, $$..$$, \(..\), and \[..\]) are also extracted.
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


def _last_unclosed_pair_start(
    text: str,
    opener: str,
    closer: str,
) -> int | None:
    """Return the last unmatched explicit TeX opener, if one exists."""
    opened_at: int | None = None
    cursor = 0
    while cursor < len(text):
        next_open = text.find(opener, cursor)
        next_close = text.find(closer, cursor)
        if next_open < 0 and next_close < 0:
            break
        if next_open >= 0 and (next_close < 0 or next_open < next_close):
            opened_at = next_open
            cursor = next_open + len(opener)
            continue
        if opened_at is not None:
            opened_at = None
        cursor = next_close + len(closer)
    return opened_at


def split_settled(
    text: str,
    *,
    adaptive_math: bool = False,
) -> tuple[str, str, bool]:
    r"""Split a streaming-in-progress response into (settled, pending, open).

    ``settled``  — leading prefix in which every renderable block ($$..$$,
                   $..$, \[...\], \(...\), Markdown table) is fully closed.
                   Safe to finalize:
                   parse_segments() + render block images, immutable thereafter.
    ``pending``  — trailing region that may still grow.
    ``open``     — True when ``pending`` begins with an *unclosed renderable
                   block* (open math delimiter or a table still
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

    # Codex commonly emits standard TeX delimiters instead of dollar signs.
    # Hold an incomplete explicit block exactly like an incomplete $ block so
    # raw TeX never flashes in Discord before the image is ready.
    for opener, closer in ((r"\[", r"\]"), (r"\(", r"\)")):
        pos = _last_unclosed_pair_start(masked, opener, closer)
        if pos is not None:
            open_start = pos if open_start is None else min(open_start, pos)

    # A streaming tick can stop after the first character of ``$$``, ``\[``,
    # or ``\(``. Hold that ambiguous suffix for one tick so it cannot flash as
    # prose before the next token reveals the complete opener. A trailing ``$``
    # that already closes a complete expression is not ambiguous.
    if scan_limit:
        suffix = masked[scan_limit - 1:scan_limit]
        if suffix in {"$", "\\"}:
            closes_math = any(
                match.end() == scan_limit for match in _iter_math_matches(masked)
            )
            trailing_backslashes = len(masked) - len(masked.rstrip("\\"))
            if (
                (suffix == "\\" and trailing_backslashes % 2 == 1)
                or (suffix == "$" and not closes_math)
            ):
                pos = scan_limit - 1
                open_start = pos if open_start is None else min(open_start, pos)

    # Unclosed inline math: odd number of single $ after neutralizing $$ pairs.
    # Two guards so we don't hold on a literal $ (currency):
    #   - newline after the open $ → literal $ (e.g. a price on its own line)
    #   - the tail after the open $ has neither TeX syntax nor a symbolic
    #     identifier prefix → it is currency like "$50", not math being typed.
    masked_no_dd = re.sub(r"\$\$", "  ", masked)
    singles = [m.start() for m in re.finditer(r"\$", masked_no_dd)]
    if len(singles) % 2 == 1:
        pos = singles[-1]
        tail = text[pos:scan_limit]
        open_body = tail[1:]
        looks_symbolic = bool(re.match(r"[A-Za-z\u0370-\u03ff\\]", open_body))
        if "\n" not in tail and (
            _INLINE_TEX_MARKER_RE.search(open_body) or looks_symbolic
        ):
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
    for m in _iter_renderable_math_matches(
        masked, adaptive_math=adaptive_math,
    ):
        last_end = max(last_end, m.end())
    # text[last_end:] is plain prose (plus any open code fence region after
    # scan_limit, which is also live text) — stream it as an editable draft.
    return text[:last_end], text[last_end:], False


def first_block_span(
    text: str,
    *,
    adaptive_math: bool = False,
) -> tuple[int, int, "Segment"] | None:
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
    mm = next(
        _iter_renderable_math_matches(
            masked, adaptive_math=adaptive_math,
        ),
        None,
    )
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

    return start, end, _math_segment(raw)

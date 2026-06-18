"""Structural-aware chunker for invest_kb.

Goals:
- Respect markdown headings: never split mid-heading; a heading + its
  immediately-following paragraph stays together when possible.
- Respect sentence boundaries: split on 。/./? where possible, never mid-word.
- Don't split fenced code blocks.
- Target window with overlap so neighbouring chunks share context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Sentence-ending punctuation, JP + EN.
_JP_SENT = re.compile(r"(?<=[。．！？\?!])\s*")
_EN_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_FENCE = re.compile(r"```[\s\S]*?```")


@dataclass
class Chunk:
    text: str
    start: int
    end: int

    def __len__(self) -> int:
        return len(self.text)


def _protect_fences(text: str) -> tuple[str, list[str]]:
    """Replace fenced code blocks with placeholders so the splitter does not
    cut them in half. Returns (masked_text, ordered_fences)."""
    fences: list[str] = []

    def _take(m):
        fences.append(m.group(0))
        return f"\x00FENCE_{len(fences) - 1}\x00"

    return _FENCE.sub(_take, text), fences


def _restore_fences(text: str, fences: list[str]) -> str:
    for i, fence in enumerate(fences):
        text = text.replace(f"\x00FENCE_{i}\x00", fence)
    return text


def _split_sentences(text: str, lang: str) -> list[str]:
    """Yield sentence-sized fragments. Always returns at least one fragment."""
    if not text:
        return []
    if lang == "ja":
        parts = [p for p in _JP_SENT.split(text) if p.strip()]
    else:
        parts = [p for p in _EN_SENT.split(text) if p.strip()]
    return parts or [text]


def _is_heading(line: str) -> bool:
    return bool(_MD_HEADING.match(line.lstrip()))


def chunk_text(
    text: str,
    *,
    target: int = 800,
    max_size: int = 1200,
    overlap: int = 120,
    lang: str = "ja",
) -> list[Chunk]:
    """Split text into target-sized chunks at structural boundaries.

    Returned chunks always satisfy `len(c) <= max_size`. They may be shorter
    when a heading or a single oversize sentence forces an early break.
    """
    if not text or not text.strip():
        return []
    if max_size < target:
        raise ValueError("max_size must be >= target")
    masked, fences = _protect_fences(text)
    # First pass: blocks bounded by blank lines (paragraphs / heading groups).
    blocks = [b for b in re.split(r"\n\s*\n", masked) if b.strip()]
    # Second pass: flatten paragraphs into sentences while keeping heading
    # lines glued to the next paragraph (so a chunk that opens with a heading
    # also contains its content).
    units: list[str] = []
    pending_heading: str = ""
    for block in blocks:
        block = block.strip()
        # If the block IS just a heading (single line starting with #), defer it
        if "\n" not in block and _is_heading(block):
            if pending_heading:
                units.append(pending_heading)
            pending_heading = block
            continue
        if pending_heading:
            block = pending_heading + "\n" + block
            pending_heading = ""
        # Single-line oversized block: sentence-split.
        if len(block) > target:
            sents = _split_sentences(block, lang)
            buf = ""
            for s in sents:
                if not buf:
                    buf = s
                elif len(buf) + len(s) + 1 <= target:
                    buf = buf + (" " if lang != "ja" else "") + s
                else:
                    units.append(buf)
                    buf = s
            if buf:
                units.append(buf)
        else:
            units.append(block)
    if pending_heading:
        units.append(pending_heading)
    # Third pass: greedy pack units into chunks within [target, max_size].
    chunks: list[Chunk] = []
    cursor = 0
    buf = ""
    buf_start = 0
    for u in units:
        if not buf:
            buf = u
            buf_start = cursor
            cursor += len(u) + 2  # +2 for stripped blank line
            continue
        joined = buf + "\n\n" + u
        if len(joined) <= max_size:
            buf = joined
            cursor += len(u) + 2
            continue
        # Flush current buf
        chunks.append(Chunk(text=buf, start=buf_start, end=buf_start + len(buf)))
        # Carry tail of buf as overlap so the next chunk shares context.
        tail = buf[-overlap:] if overlap and overlap < len(buf) else ""
        # Restore from tail boundary to a sentence break to avoid mid-word.
        if tail:
            for sep in ("。", "！", "？", "\n", ". ", "! ", "? "):
                pos = tail.rfind(sep)
                if pos != -1:
                    tail = tail[pos + len(sep) :].lstrip()
                    break
        buf = (tail + "\n\n" + u) if tail else u
        buf_start = chunks[-1].end - len(tail) if tail else cursor
        cursor += len(u) + 2
    if buf:
        chunks.append(Chunk(text=buf, start=buf_start, end=buf_start + len(buf)))
    # Restore fences.
    for c in chunks:
        c.text = _restore_fences(c.text, fences)
    # Filter empty after restore.
    return [c for c in chunks if c.text.strip()]

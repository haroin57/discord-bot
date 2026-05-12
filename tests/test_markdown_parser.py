"""Tests for utils/markdown_parser.py"""

import pytest

from utils.markdown_parser import (
    MathSegment,
    TableSegment,
    TextSegment,
    has_math,
    has_tables,
    mask_code_blocks,
    parse_segments,
)


# ---------------------------------------------------------------------------
# mask_code_blocks
# ---------------------------------------------------------------------------

class TestMaskCodeBlocks:
    def test_replaces_code_block_content_with_spaces(self):
        text = "before\n```python\nx = 1\n```\nafter"
        result = mask_code_blocks(text)
        assert "x = 1" not in result
        assert "before" in result
        assert "after" in result

    def test_preserves_newlines_in_code_block(self):
        text = "```\nline1\nline2\n```"
        result = mask_code_blocks(text)
        assert result.count("\n") == text.count("\n")

    def test_no_code_block_unchanged(self):
        text = "just plain text"
        assert mask_code_blocks(text) == text

    def test_empty_string(self):
        assert mask_code_blocks("") == ""

    def test_multiple_code_blocks(self):
        text = "```a\ncode1\n```\nmiddle\n```b\ncode2\n```"
        result = mask_code_blocks(text)
        assert "code1" not in result
        assert "code2" not in result
        assert "middle" in result

    def test_code_block_same_length(self):
        text = "```python\nx = 1\n```"
        result = mask_code_blocks(text)
        assert len(result) == len(text)


# ---------------------------------------------------------------------------
# has_tables
# ---------------------------------------------------------------------------

class TestHasTables:
    def test_simple_table_detected(self):
        table = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        assert has_tables(table) is True

    def test_no_table(self):
        assert has_tables("just text") is False

    def test_empty_string(self):
        assert has_tables("") is False

    def test_table_inside_code_block_ignored(self):
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        assert has_tables(text) is False

    def test_table_with_alignment(self):
        table = "| Left | Center | Right |\n|:---|:---:|---:|\n| a | b | c |\n"
        assert has_tables(table) is True

    def test_precomputed_masked_accepted(self):
        table = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        masked = mask_code_blocks(table)
        assert has_tables(table, _masked=masked) is True

    def test_separator_only_no_table(self):
        assert has_tables("| A | B |") is False


# ---------------------------------------------------------------------------
# has_math
# ---------------------------------------------------------------------------

class TestHasMath:
    def test_display_math_detected(self):
        assert has_math("$$x^2$$") is True

    def test_display_math_multiline(self):
        text = "$$\nx^2 + y^2\n$$"
        assert has_math(text) is True

    def test_inline_math_detected(self):
        assert has_math("The value is $x + 1$ here.") is True

    def test_no_math(self):
        assert has_math("just plain text") is False

    def test_empty_string(self):
        assert has_math("") is False

    def test_math_inside_code_block_ignored(self):
        text = "```\n$$x^2$$\n```"
        assert has_math(text) is False

    def test_precomputed_masked_accepted(self):
        text = "$$x^2$$"
        masked = mask_code_blocks(text)
        assert has_math(text, _masked=masked) is True

    def test_double_dollar_not_inline(self):
        # $$ should not be counted as two inline $ expressions
        text = "$$x$$"
        assert has_math(text) is True


# ---------------------------------------------------------------------------
# parse_segments — text only
# ---------------------------------------------------------------------------

class TestParseSegmentsTextOnly:
    def test_plain_text_returns_single_text_segment(self):
        segs = parse_segments("hello world")
        assert len(segs) == 1
        assert isinstance(segs[0], TextSegment)
        assert segs[0].content == "hello world"

    def test_empty_string_returns_empty_list(self):
        assert parse_segments("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert parse_segments("   \n  ") == []


# ---------------------------------------------------------------------------
# parse_segments — tables
# ---------------------------------------------------------------------------

class TestParseSegmentsTable:
    TABLE = "| A | B |\n|---|---|\n| 1 | 2 |\n"

    def test_standalone_table_returns_table_segment(self):
        segs = parse_segments(self.TABLE)
        assert any(isinstance(s, TableSegment) for s in segs)

    def test_table_markdown_preserved(self):
        segs = parse_segments(self.TABLE)
        ts = next(s for s in segs if isinstance(s, TableSegment))
        assert "| A | B |" in ts.markdown

    def test_text_before_table(self):
        text = "Intro text.\n" + self.TABLE
        segs = parse_segments(text)
        assert isinstance(segs[0], TextSegment)
        assert isinstance(segs[1], TableSegment)

    def test_text_after_table(self):
        text = self.TABLE + "Outro text."
        segs = parse_segments(text)
        assert isinstance(segs[-1], TextSegment)

    def test_heading_before_table_becomes_caption(self):
        text = "### My Table\n" + self.TABLE
        segs = parse_segments(text)
        ts = next(s for s in segs if isinstance(s, TableSegment))
        assert ts.caption == "My Table"

    def test_heading_not_included_in_text_segment(self):
        text = "Intro\n### Caption\n" + self.TABLE
        segs = parse_segments(text)
        text_segs = [s for s in segs if isinstance(s, TextSegment)]
        assert not any("Caption" in s.content for s in text_segs)

    def test_table_inside_code_block_not_extracted(self):
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        segs = parse_segments(text)
        assert not any(isinstance(s, TableSegment) for s in segs)

    def test_multiple_tables(self):
        two_tables = self.TABLE + "\nSome text\n\n" + self.TABLE
        segs = parse_segments(two_tables)
        table_segs = [s for s in segs if isinstance(s, TableSegment)]
        assert len(table_segs) == 2

    def test_empty_caption_when_no_heading(self):
        segs = parse_segments(self.TABLE)
        ts = next(s for s in segs if isinstance(s, TableSegment))
        assert ts.caption == ""


# ---------------------------------------------------------------------------
# parse_segments — math
# ---------------------------------------------------------------------------

class TestParseSegmentsMath:
    def test_display_math_extracted(self):
        segs = parse_segments("$$x^2$$")
        assert any(isinstance(s, MathSegment) and s.display_mode for s in segs)

    def test_display_math_latex_stripped_of_delimiters(self):
        segs = parse_segments("$$x^2$$")
        ms = next(s for s in segs if isinstance(s, MathSegment))
        assert ms.latex == "x^2"
        assert "$$" not in ms.latex

    def test_inline_math_extracted(self):
        segs = parse_segments("Value is $x+1$ here.")
        assert any(isinstance(s, MathSegment) and not s.display_mode for s in segs)

    def test_inline_math_latex_stripped_of_delimiters(self):
        segs = parse_segments("Value is $x+1$ here.")
        ms = next(s for s in segs if isinstance(s, MathSegment))
        assert "$" not in ms.latex

    def test_text_around_math_preserved(self):
        segs = parse_segments("Before $$E=mc^2$$ after.")
        types = [type(s).__name__ for s in segs]
        assert "TextSegment" in types
        assert "MathSegment" in types

    def test_math_inside_code_block_not_extracted(self):
        text = "```\n$$x^2$$\n```"
        segs = parse_segments(text)
        assert not any(isinstance(s, MathSegment) for s in segs)

    def test_multiline_display_math(self):
        text = "$$\nx^2 + y^2 = r^2\n$$"
        segs = parse_segments(text)
        ms_list = [s for s in segs if isinstance(s, MathSegment)]
        assert len(ms_list) == 1
        assert ms_list[0].display_mode is True

    def test_multiple_inline_math(self):
        text = "Let $a$ and $b$ be integers."
        segs = parse_segments(text)
        math_segs = [s for s in segs if isinstance(s, MathSegment)]
        assert len(math_segs) == 2


# ---------------------------------------------------------------------------
# parse_segments — combined
# ---------------------------------------------------------------------------

class TestParseSegmentsCombined:
    TABLE = "| X | Y |\n|---|---|\n| 1 | 2 |\n"

    def test_text_table_math_all_present(self):
        text = "Intro $a+b$ text.\n### Cap\n" + self.TABLE + "\nFinal $$z$$\n"
        segs = parse_segments(text)
        assert any(isinstance(s, TextSegment) for s in segs)
        assert any(isinstance(s, TableSegment) for s in segs)
        assert any(isinstance(s, MathSegment) for s in segs)

    def test_segment_order_preserved(self):
        text = "Text before.\n" + self.TABLE + "\nText after."
        segs = parse_segments(text)
        first_type = type(segs[0]).__name__
        table_idx = next(i for i, s in enumerate(segs) if isinstance(s, TableSegment))
        assert first_type == "TextSegment"
        assert table_idx > 0

    def test_code_block_before_table(self):
        text = "```python\n| A | B |\n|---|---|\n| 1 | 2 |\n```\n" + self.TABLE
        segs = parse_segments(text)
        table_segs = [s for s in segs if isinstance(s, TableSegment)]
        assert len(table_segs) == 1  # Only the real table, not the one in code block

    def test_no_duplicate_content(self):
        text = "Unique text\n" + self.TABLE
        segs = parse_segments(text)
        texts = " ".join(s.content for s in segs if isinstance(s, TextSegment))
        assert texts.count("Unique text") == 1

"""Tests for utils/markdown_parser.py"""

import pytest

from utils.markdown_parser import (
    MathSegment,
    TableSegment,
    TextSegment,
    first_block_span,
    format_math_for_discord,
    has_math,
    has_tables,
    mask_code_blocks,
    parse_segments,
    split_settled,
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
        assert has_math("The value is $x = 1$ here.") is True

    def test_tex_display_delimiter_detected(self):
        assert has_math(r"The matrix is \[A=I\].") is True

    def test_tex_inline_delimiter_detected(self):
        assert has_math(r"The value is \(x=1\) here.") is True

    def test_single_variable_detected(self):
        assert has_math("Let $n$ be natural and $A$ a matrix.") is True

    def test_inline_currency_not_detected(self):
        # Regression: plain currency must NOT be treated as inline math.
        assert has_math("価格は$50、送料$10") is False
        assert has_math("$5から$10です") is False
        assert has_math("$100-$200") is False
        assert has_math("from $5 to $10") is False

    def test_no_math(self):
        assert has_math("just plain text") is False

    def test_empty_string(self):
        assert has_math("") is False

    def test_math_inside_code_block_ignored(self):
        text = "```\n$$x^2$$\n```"
        assert has_math(text) is False

    def test_tex_delimiters_inside_code_block_ignored(self):
        text = "```tex\n\\[x^2\\]\n\\(y\\)\n```"
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
        segs = parse_segments("Value is $x=1$ here.")
        assert any(isinstance(s, MathSegment) and not s.display_mode for s in segs)

    def test_inline_math_latex_stripped_of_delimiters(self):
        segs = parse_segments("Value is $x=1$ here.")
        ms = next(s for s in segs if isinstance(s, MathSegment))
        assert "$" not in ms.latex

    def test_tex_display_math_extracted_and_stripped(self):
        segs = parse_segments(r"Before \[x^2+y^2=r^2\] after.")
        ms = next(s for s in segs if isinstance(s, MathSegment))
        assert ms.display_mode is True
        assert ms.latex == "x^2+y^2=r^2"

    def test_tex_inline_math_extracted_and_stripped(self):
        segs = parse_segments(r"Value \(A_{jk}=B_{jk}\) follows.")
        ms = next(s for s in segs if isinstance(s, MathSegment))
        assert ms.display_mode is False
        assert ms.latex == r"A_{jk}=B_{jk}"

    def test_codex_matrix_response_delimiters_are_all_extracted(self):
        text = r"""
厳密には、元の行列 \(A\) の第 \(j\) 行による展開ではありません。

\[
B=
\begin{pmatrix}
 & & \\
a_{i1}&\cdots&a_{in} \quad \leftarrow j\text{行目}\\
 & &
\end{pmatrix}
\]

したがって、\[C_{ij}=|B|=0\] です。
"""
        math_segs = [s for s in parse_segments(text) if isinstance(s, MathSegment)]
        assert len(math_segs) == 4
        assert [s.display_mode for s in math_segs] == [False, False, True, True]
        assert r"\begin{pmatrix}" in math_segs[2].latex

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
        text = "Let $a=1$ and $b=2$ be integers."
        segs = parse_segments(text)
        math_segs = [s for s in segs if isinstance(s, MathSegment)]
        assert len(math_segs) == 2

    def test_adjacent_simple_and_complex_math_are_not_merged(self):
        text = r"すべての自然数 $n$ について $A^n=\begin{pmatrix}1&0\\0&1\end{pmatrix}$ を示す。"
        math_segs = [s for s in parse_segments(text) if isinstance(s, MathSegment)]
        assert [s.latex for s in math_segs] == [
            "n",
            r"A^n=\begin{pmatrix}1&0\\0&1\end{pmatrix}",
        ]

    def test_multiple_markerless_variables_are_separate(self):
        text = "Let $x$ and $y$ be variables."
        math_segs = [s for s in parse_segments(text) if isinstance(s, MathSegment)]
        assert [s.latex for s in math_segs] == ["x", "y"]


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


# ---------------------------------------------------------------------------
# split_settled — incremental streaming split (B)
# ---------------------------------------------------------------------------

class TestSplitSettled:
    TABLE = "| X | Y |\n|---|---|\n| 1 | 2 |\n"

    def test_invariant_settled_plus_pending_equals_text(self):
        for text in ("", "hello", "a $$x$$ b", "row\n" + self.TABLE,
                     "open $$x", "open $y", "```py\ncode"):
            settled, pending, _ = split_settled(text)
            assert settled + pending == text

    def test_empty(self):
        assert split_settled("") == ("", "", False)

    def test_plain_prose_is_all_pending_prose(self):
        # No blocks → nothing settled, the whole thing streams live as prose.
        settled, pending, is_open = split_settled("just some prose being typed")
        assert settled == ""
        assert pending == "just some prose being typed"
        assert is_open is False

    def test_closed_display_math_is_settled(self):
        text = "before\n\n$$x = y$$\n\nafter prose"
        settled, pending, is_open = split_settled(text)
        # The closed $$ and the text before it are settled; trailing prose pends.
        assert "$$x = y$$" in settled
        assert pending.strip() == "after prose"
        assert is_open is False

    def test_open_display_math_is_held(self):
        text = "intro text\n\n$$x = "
        settled, pending, is_open = split_settled(text)
        assert settled == "intro text\n\n"
        assert pending == "$$x = "
        assert is_open is True

    @pytest.mark.parametrize("suffix", ["$", "\\"])
    def test_ambiguous_streaming_opener_prefix_is_held(self, suffix):
        text = "intro " + suffix
        settled, pending, is_open = split_settled(text)
        assert settled == "intro "
        assert pending == suffix
        assert is_open is True

    def test_complete_trailing_inline_math_is_not_mistaken_for_opener(self):
        text = "value $x$"
        settled, pending, is_open = split_settled(text)
        assert settled == text
        assert pending == ""
        assert is_open is False

    def test_even_trailing_backslashes_are_not_an_opener_prefix(self):
        text = r"literal line break \\"
        settled, pending, is_open = split_settled(text)
        assert settled == ""
        assert pending == text
        assert is_open is False

    def test_open_tex_display_math_is_held(self):
        text = r"intro text \[A=\begin{pmatrix}1&0"
        settled, pending, is_open = split_settled(text)
        assert settled == "intro text "
        assert pending.startswith(r"\[")
        assert is_open is True

    def test_closed_tex_display_math_is_settled(self):
        text = r"before \[A=I\] after prose"
        settled, pending, is_open = split_settled(text)
        assert r"\[A=I\]" in settled
        assert pending.strip() == "after prose"
        assert is_open is False

    def test_open_inline_math_is_held(self):
        text = "the value $x = "
        settled, pending, is_open = split_settled(text)
        assert settled == "the value "
        assert pending == "$x = "
        assert is_open is True

    def test_open_tex_inline_math_is_held(self):
        text = r"the value \(x = "
        settled, pending, is_open = split_settled(text)
        assert settled == "the value "
        assert pending == r"\(x = "
        assert is_open is True

    def test_closed_inline_math_is_settled(self):
        text = "the value $x=y$ is set, and more"
        settled, pending, is_open = split_settled(text)
        assert "$x=y$" in settled
        assert "and more" in pending
        assert is_open is False

    def test_adaptive_simple_math_remains_in_live_prose(self):
        text = "the value \\(x=0\\) is final"
        settled, pending, is_open = split_settled(text, adaptive_math=True)
        assert settled == ""
        assert pending == text
        assert is_open is False

    def test_adaptive_complex_math_is_still_settled(self):
        text = r"before \[\frac{a}{b}\] after"
        settled, pending, is_open = split_settled(text, adaptive_math=True)
        assert r"\[\frac{a}{b}\]" in settled
        assert pending.strip() == "after"
        assert is_open is False

    def test_lone_dollar_with_newline_not_held(self):
        # A price like "$5\n..." is a literal $, not open inline math.
        text = "it costs $5 today\nand tomorrow too"
        settled, pending, is_open = split_settled(text)
        assert is_open is False
        # Everything streams as prose (no hold).
        assert settled == ""
        assert pending == text

    def test_even_dollars_not_held(self):
        text = "from $5 to $10 range"  # two literal $ → even → no hold
        _, _, is_open = split_settled(text)
        assert is_open is False

    def test_growing_table_is_held_from_its_start(self):
        # Header + separator + one row, nothing after → table may still grow.
        text = "Here is data:\n\n" + self.TABLE
        settled, pending, is_open = split_settled(text)
        assert settled == "Here is data:\n\n"
        assert pending == self.TABLE
        assert is_open is True

    def test_table_followed_by_prose_is_settled(self):
        text = "intro\n\n" + self.TABLE + "\nconclusion line"
        settled, pending, is_open = split_settled(text)
        assert self.TABLE.strip() in settled
        assert "conclusion line" in pending
        assert is_open is False

    def test_trailing_prose_after_block_is_pending(self):
        # The growing prose run after a closed block must NOT be settled — it's
        # the live draft and still accepts more characters.
        text = "lead\n\n$$x$$\n\nthis tail is still being typed"
        settled, pending, is_open = split_settled(text)
        assert "$$x$$" in settled
        assert "still being typed" in pending
        assert is_open is False

    def test_partial_table_header_only_is_held(self):
        # Just the header line typed so far — hold before the raw row flashes.
        text = "data:\n\n| A | B |\n"
        settled, pending, is_open = split_settled(text)
        assert settled == "data:\n\n"
        assert pending == "| A | B |\n"
        assert is_open is True

    def test_partial_table_header_and_separator_held(self):
        text = "data:\n\n| A | B |\n|---|---|\n"
        settled, pending, is_open = split_settled(text)
        assert settled == "data:\n\n"
        assert is_open is True

    def test_table_then_blank_line_is_settled(self):
        # A blank line terminates the table → settled even with no prose after.
        text = "x\n\n" + self.TABLE + "\n"
        settled, pending, is_open = split_settled(text)
        assert self.TABLE.strip() in settled
        assert is_open is False

    def test_open_code_fence_is_prose_not_block(self):
        # Code being typed streams live as text, not held as a render block.
        # With no closed render block before it, the whole thing is one live
        # prose run (settled empty).
        text = "explanation\n\n```python\nx = 1\n"
        settled, pending, is_open = split_settled(text)
        assert is_open is False
        assert "```python" in pending
        assert settled == ""

    def test_dollar_inside_closed_code_not_counted(self):
        # A $ inside a CLOSED code block must not look like open inline math.
        # No real render block → all one live prose run, no hold.
        text = "```bash\necho $HOME\n```\nplain tail"
        settled, pending, is_open = split_settled(text)
        assert is_open is False
        assert pending == text

    def test_dollar_inside_open_code_fence_not_counted(self):
        # $ inside an OPEN code fence is code being typed, not open math.
        text = "intro\n\n```bash\necho $HOME"
        settled, pending, is_open = split_settled(text)
        assert is_open is False
        assert "```bash" in pending

    def test_table_inside_code_not_held(self):
        # Pipes inside a closed code block aren't a table → no hold.
        text = "```\n| not | a | table |\n```\nreal tail"
        settled, pending, is_open = split_settled(text)
        assert is_open is False
        assert pending == text

    def test_two_closed_blocks_both_settled(self):
        text = "a\n\n$$p$$\n\nb\n\n" + self.TABLE + "\ntail"
        settled, pending, is_open = split_settled(text)
        assert "$$p$$" in settled
        assert self.TABLE.strip() in settled
        assert "tail" in pending
        assert is_open is False

    def test_settled_prefix_grows_monotonically(self):
        # As the stream grows, the settled prefix never shrinks (stable
        # finalization — earlier settled content stays settled).
        steps = [
            "Intro ",
            "Intro text.\n\n$$a+b",
            "Intro text.\n\n$$a+b$$\n\n",
            "Intro text.\n\n$$a+b$$\n\nmore prose",
            "Intro text.\n\n$$a+b$$\n\nmore prose\n\n" + self.TABLE,
        ]
        prev_len = 0
        for s in steps:
            settled, _, _ = split_settled(s)
            assert len(settled) >= prev_len
            prev_len = len(settled)

    def test_settled_is_parseable_into_closed_segments(self):
        # Whatever lands in `settled` must round-trip through parse_segments
        # with all math/tables closed (the finalize step relies on this).
        text = "lead $a=1$ mid\n\n$$Q$$\n\n" + self.TABLE + "\nopen $$z"
        settled, pending, is_open = split_settled(text)
        assert pending == "$$z"
        assert is_open is True
        segs = parse_segments(settled)
        # The two math + one table closed in `settled` are detected.
        assert any(isinstance(s, TableSegment) for s in segs)
        assert sum(isinstance(s, MathSegment) for s in segs) >= 2


# ---------------------------------------------------------------------------
# first_block_span — leftmost closed block for the incremental streamer (B)
# ---------------------------------------------------------------------------

class TestFirstBlockSpan:
    TABLE = "| X | Y |\n|---|---|\n| 1 | 2 |\n"

    def test_none_when_no_block(self):
        assert first_block_span("just plain prose, no blocks") is None

    def test_finds_display_math_with_offsets(self):
        text = "lead in $$E=mc^2$$ trailing"
        span = first_block_span(text)
        assert span is not None
        start, end, seg = span
        assert text[start:end] == "$$E=mc^2$$"
        assert isinstance(seg, MathSegment)
        assert seg.display_mode is True
        assert seg.latex == "E=mc^2"

    def test_finds_inline_math(self):
        text = "value $a=b$ here"
        start, end, seg = first_block_span(text)
        assert text[start:end] == "$a=b$"
        assert isinstance(seg, MathSegment)
        assert seg.display_mode is False
        assert seg.latex == "a=b"

    def test_finds_tex_display_math(self):
        text = r"lead in \[E=mc^2\] trailing"
        start, end, seg = first_block_span(text)
        assert text[start:end] == r"\[E=mc^2\]"
        assert isinstance(seg, MathSegment)
        assert seg.display_mode is True
        assert seg.latex == "E=mc^2"

    def test_finds_tex_inline_math(self):
        text = r"value \(a=b\) here"
        start, end, seg = first_block_span(text)
        assert text[start:end] == r"\(a=b\)"
        assert isinstance(seg, MathSegment)
        assert seg.display_mode is False
        assert seg.latex == "a=b"

    def test_finds_table_with_offsets(self):
        text = "before\n\n" + self.TABLE + "\nafter"
        start, end, seg = first_block_span(text)
        assert isinstance(seg, TableSegment)
        assert text[start:end].strip().startswith("| X | Y |")

    def test_leftmost_block_wins(self):
        text = "x $a=1$ then\n\n" + self.TABLE
        start, end, seg = first_block_span(text)
        # The inline math comes before the table → it's returned first.
        assert isinstance(seg, MathSegment)
        assert text[start:end] == "$a=1$"

    def test_table_absorbs_preceding_heading_as_caption(self):
        text = "intro\n\n### My Caption\n" + self.TABLE
        start, end, seg = first_block_span(text)
        assert isinstance(seg, TableSegment)
        assert seg.caption == "My Caption"
        # The heading is folded into the span (not left as dangling prose).
        assert "### My Caption" in text[start:end]
        # Prose before the span is just the intro.
        assert text[:start].strip() == "intro"

    def test_ignores_block_inside_code_fence(self):
        text = "```\n$$not math$$\n```\nreal $x=1$ here"
        start, end, seg = first_block_span(text)
        # The $$ inside the code fence is masked; the real inline $x=1$ wins.
        assert text[start:end] == "$x=1$"

    def test_adaptive_skips_simple_math_and_finds_complex_math(self):
        text = r"value \(x=0\), then \[\frac{a}{b}\]"
        start, end, seg = first_block_span(text, adaptive_math=True)
        assert text[start:end] == r"\[\frac{a}{b}\]"
        assert isinstance(seg, MathSegment)

    def test_adaptive_simple_math_is_not_an_image_block(self):
        assert first_block_span(r"value \(x=0\)", adaptive_math=True) is None


class TestFormatMathForDiscord:
    def test_rewrites_simple_math_in_place(self):
        text = r"vectors \(a_1,\ldots,a_n\) give \(x\ne0\)."
        result = format_math_for_discord(text)
        assert result == "vectors a₁, …, aₙ give x ≠ 0."

    def test_preserves_complex_math_for_image_pipeline(self):
        text = r"before \[\frac{a}{b}\] after"
        assert format_math_for_discord(text) == text

    def test_does_not_rewrite_tex_inside_code_fence(self):
        text = "```tex\n\\(x=0\\)\n```\nreal \\(y=1\\)"
        result = format_math_for_discord(text)
        assert "\\(x=0\\)" in result
        assert result.endswith("real y = 1")


# ---------------------------------------------------------------------------
# Streaming composition: replay split_settled + first_block_span over growing
# prefixes the way bot.py's incremental renderer does, and assert the two
# guarantees the renderer relies on:
#   1. Every block is emitted exactly once, in order (== parse_segments).
#   2. A raw block NEVER leaks into the live-prose region (no flash-then-delete).
# ---------------------------------------------------------------------------

def _replay_stream(full_text: str, step: int):
    """Mirror _render_incremental's block-emission + live-prose logic."""
    emitted = 0
    blocks: list = []
    live_snapshots: list[str] = []
    n = len(full_text)
    cut = step
    while True:
        final = cut >= n
        prefix = full_text[:n if final else cut]
        if final:
            settled_end, is_open = len(prefix), False
        else:
            settled, _pending, is_open = split_settled(prefix)
            settled_end = len(settled)
        # emit every closed block in [emitted, settled_end]
        guard = 0
        while guard < 1000:
            guard += 1
            region = prefix[emitted:settled_end]
            span = first_block_span(region)
            if span is None:
                break
            start, end, seg = span
            blocks.append(seg)
            emitted += end
        if final:
            break
        live = prefix[emitted:settled_end] if is_open else prefix[emitted:]
        live_snapshots.append(live)
        cut += step
    return blocks, live_snapshots


class TestStreamingComposition:
    TABLE = "| X | Y |\n|---|---|\n| 1 | 2 |\n"
    CASES = [
        "Just plain prose with no blocks at all, streamed in.",
        "Intro text.\n\n$$E = mc^2$$\n\nThen a closing remark.",
        "Lead.\n\nHere is $a + b$ inline and more prose after it.",
        "See the data:\n\n" + TABLE + "\nThat concludes the table.",
        "Mix: $x$ then\n\n$$y=1$$\n\n" + TABLE + "\n\nfinal words.",
        r"Codex form: \(x\) then \[A=\begin{pmatrix}1&0\\0&1\end{pmatrix}\] done.",
        "### Caption\n" + TABLE + "\nafter caption table.",
        "Cost is $5 today, around $10 tomorrow — no math here.",
        "```python\nx = '$$ not math $$'\n| not | table |\n```\nreal tail.",
    ]

    def _expected_blocks(self, text):
        return [
            (type(s).__name__,
             getattr(s, "latex", getattr(s, "markdown", "")).strip())
            for s in parse_segments(text)
            if isinstance(s, (TableSegment, MathSegment))
        ]

    def test_all_blocks_emitted_in_order(self):
        for text in self.CASES:
            for step in (1, 3, 7, 50):
                blocks, _ = _replay_stream(text, step)
                got = [
                    (type(s).__name__,
                     getattr(s, "latex", getattr(s, "markdown", "")).strip())
                    for s in blocks
                ]
                assert got == self._expected_blocks(text), (text, step, got)

    def test_no_raw_block_leaks_into_live_prose(self):
        # The live-prose region the renderer drafts must never contain a block
        # that split_settled considers EMITTABLE (a closed table/math outside an
        # open code fence) — that's the flash-then-delete the rewrite removes.
        # (has_math/has_tables would false-positive on $$ typed inside an open
        # code fence, which is legitimate raw code, not a render block — so we
        # use the fence-aware split_settled to define "emittable".)
        for text in self.CASES:
            for step in (1, 3, 7, 50):
                _, snapshots = _replay_stream(text, step)
                for snap in snapshots:
                    settled_part = split_settled(snap)[0]
                    assert first_block_span(settled_part) is None, (text, step, snap)

    def test_blocks_emitted_once_no_duplicates(self):
        text = "a\n\n$$p$$\n\nb\n\n" + self.TABLE + "\n\nc $q$ d"
        for step in (1, 2, 5, 13):
            blocks, _ = _replay_stream(text, step)
            assert len(blocks) == len(self._expected_blocks(text))

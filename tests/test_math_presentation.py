"""Focused tests for adaptive Discord math presentation."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from utils.markdown_parser import MathSegment, TableSegment, TextSegment, parse_segments
from utils.math_presentation import (
    MathPresentation,
    PresentationBlockKind,
    PresentationKind,
    classify_math,
    latex_to_discord_text,
    plan_segments,
)


@pytest.mark.parametrize(
    ("latex", "expected"),
    [
        ("A", "A"),
        ("0", "0"),
        ("r", "r"),
        (r"a_1,\ldots,a_n", "a₁, …, aₙ"),
        (r"\boldsymbol{a}_1,\ldots,\boldsymbol{a}_n", "a₁, …, aₙ"),
        (r"\boldsymbol{x}\ne\boldsymbol{0}", "x ≠ 0"),
        (r"x=0", "x = 0"),
        (r"m\times n", "m × n"),
        (r"\dim\ker A", "dim ker A"),
        (r"\operatorname{rank}A=r", "rank A = r"),
        (r"A\boldsymbol{x}=\boldsymbol{0}", "Ax = 0"),
        (r"\alpha+\beta=\gamma", "α + β = γ"),
        (r"x_i^2", "xᵢ²"),
        (r"x_{i1}=a_{n}", "xᵢ₁ = aₙ"),
        (r"\mathbb{R}^n", "ℝⁿ"),
        (r"\left(x\right)\in\mathbb{R}", "(x) ∈ ℝ"),
        (r"u\cdot v", "u · v"),
        (r"A\subseteq B", "A ⊆ B"),
        (r"n\to\infty", "n → ∞"),
        (r"\text{rank}(A)=r", "rank(A) = r"),
    ],
)
def test_safe_tex_is_normalised_to_discord_text(latex, expected):
    assert latex_to_discord_text(latex) == expected


@pytest.mark.parametrize("latex", ["A", "0", "r", "x=0", r"m\times n"])
@pytest.mark.parametrize("display_mode", [False, True])
def test_simple_math_is_text_independent_of_delimiter_style(latex, display_mode):
    result = classify_math(latex, display_mode=display_mode)

    assert isinstance(result, MathPresentation)
    assert result.kind is PresentationKind.TEXT
    assert result.reason == ("simple-display" if display_mode else "simple-inline")


@pytest.mark.parametrize(
    ("latex", "reason"),
    [
        (r"\begin{aligned}x&=1\\y&=2\end{aligned}", "environment:aligned"),
        (r"\begin{matrix}1&0\\0&1\end{matrix}", "environment:matrix"),
        (r"\begin{pmatrix}1\\2\end{pmatrix}", "environment:pmatrix"),
        (r"\begin{bmatrix}1\\2\end{bmatrix}", "environment:bmatrix"),
        (r"\begin{cases}1&x>0\\0&x=0\end{cases}", "environment:cases"),
        (r"\begin{array}{cc}a&b\end{array}", "environment:array"),
        (r"\begin{gizmo}x\end{gizmo}", "environment:gizmo"),
        (r"x\\ y", "explicit-line-break"),
        (r"x\\y", "explicit-line-break"),
        (r"x\\[2pt]y", "explicit-line-break"),
    ],
)
def test_two_dimensional_tex_stays_an_image(latex, reason):
    result = classify_math(latex)

    assert result.kind is PresentationKind.IMAGE
    assert result.text == latex
    assert result.reason == reason
    assert latex_to_discord_text(latex) is None


@pytest.mark.parametrize(
    "command",
    [
        "frac",
        "dfrac",
        "tfrac",
        "cfrac",
        "sqrt",
        "sum",
        "prod",
        "int",
        "iint",
        "iiint",
        "oint",
        "lim",
        "binom",
        "overset",
        "underset",
        "substack",
    ],
)
def test_complex_commands_stay_images(command):
    latex = rf"\{command}{{x}}"

    result = classify_math(latex)

    assert result.kind is PresentationKind.IMAGE
    assert result.reason == f"complex-command:{command}"


@pytest.mark.parametrize(
    ("latex", "reason"),
    [
        (r"\unknown{x}", "unknown-command:unknown"),
        (r"\color{red}{x}", "unknown-command:color"),
        (r"x_{i", "unbalanced-braces"),
        (r"x_i}", "unbalanced-braces"),
        ("", "empty"),
        ("   ", "empty"),
        ("x & y", "alignment-marker"),
        ("x % hidden", "tex-comment"),
        ("x\\", "malformed-command"),
        (r"x_q", "unsupported-script"),
    ],
)
def test_unsafe_or_ambiguous_tex_stays_an_image(latex, reason):
    result = classify_math(latex)

    assert result.kind is PresentationKind.IMAGE
    assert result.text == latex
    assert result.reason == reason


def test_escaped_special_characters_are_safe_text():
    assert latex_to_discord_text(r"x\_1=50\%") == "x_1 = 50%"


def test_segment_like_object_is_supported():
    @dataclass
    class SegmentLike:
        latex: str
        display_mode: bool

    result = classify_math(SegmentLike("A=0", True))

    assert result.kind is PresentationKind.TEXT
    assert result.text == "A = 0"
    assert result.reason == "simple-display"


def test_explicit_display_mode_overrides_segment_value():
    result = classify_math(MathSegment("A", display_mode=True), display_mode=False)

    assert result.reason == "simple-inline"


@pytest.mark.parametrize("bad", [None, 1, object()])
def test_classify_rejects_non_segment_objects(bad):
    with pytest.raises(TypeError, match="latex and display_mode"):
        classify_math(bad)


def test_latex_to_text_requires_string():
    with pytest.raises(TypeError, match="latex must be a string"):
        latex_to_discord_text(None)  # type: ignore[arg-type]


def test_plan_coalesces_simple_math_with_surrounding_text():
    segments = [
        TextSegment("Let "),
        MathSegment("A", display_mode=False),
        TextSegment(" satisfy "),
        MathSegment("A=0", display_mode=True),
        TextSegment("."),
    ]

    blocks = plan_segments(segments)

    assert len(blocks) == 1
    assert blocks[0].kind is PresentationBlockKind.TEXT
    assert blocks[0].text == "Let A satisfy A = 0."
    assert blocks[0].reason == "coalesced-text"
    assert blocks[0].source is None


def test_plan_preserves_parser_whitespace_without_inventing_spaces():
    blocks = plan_segments(
        [TextSegment("("), MathSegment("A", False), TextSegment(")")]
    )

    assert blocks[0].text == "(A)"


def test_plan_flushes_text_around_complex_math():
    math = MathSegment(r"\frac{1}{2}", display_mode=True)

    blocks = plan_segments([TextSegment("before"), math, TextSegment("after")])

    assert [block.kind for block in blocks] == [
        PresentationBlockKind.TEXT,
        PresentationBlockKind.MATH_IMAGE,
        PresentationBlockKind.TEXT,
    ]
    assert [block.text for block in blocks] == ["before", r"\frac{1}{2}", "after"]
    assert blocks[1].source is math
    assert blocks[1].display_mode is True
    assert blocks[1].reason == "complex-command:frac"


def test_plan_keeps_table_as_an_image_boundary():
    table = TableSegment("| A |\n|---|\n| 1 |", caption="Values")

    blocks = plan_segments([TextSegment("before"), table, TextSegment("after")])

    assert [block.kind for block in blocks] == [
        PresentationBlockKind.TEXT,
        PresentationBlockKind.TABLE_IMAGE,
        PresentationBlockKind.TEXT,
    ]
    assert blocks[1].text == table.markdown
    assert blocks[1].reason == "table"
    assert blocks[1].source is table


def test_plan_accepts_raw_text_and_drops_whitespace_only_blocks():
    blocks = plan_segments(["plain", " text", TextSegment("   ")])

    assert len(blocks) == 1
    assert blocks[0].text == "plain text   "
    assert blocks[0].reason == "plain-text"


def test_plan_empty_input_is_empty():
    assert plan_segments([]) == []


def test_plan_rejects_unknown_segment_type():
    with pytest.raises(TypeError, match="text-, math-, or table-like"):
        plan_segments([object()])


def test_plan_rejects_non_string_segment_fields():
    @dataclass
    class BadText:
        content: int

    with pytest.raises(TypeError, match="text.content must be a string"):
        plan_segments([BadText(1)])


def test_vertical_math_regression_fixture_reduces_to_one_image():
    response = (
        Path(__file__).parent / "fixtures" / "tex_inline_vertical_case.md"
    ).read_text(encoding="utf-8")
    parsed = parse_segments(response)
    planned = plan_segments(parsed)

    assert len(parsed) == 29
    assert sum(isinstance(segment, MathSegment) for segment in parsed) == 14
    assert [block.kind for block in planned] == [
        PresentationBlockKind.TEXT,
        PresentationBlockKind.MATH_IMAGE,
        PresentationBlockKind.TEXT,
    ]
    assert "a₁, …, aₙ" in planned[0].text
    assert "x ≠ 0" in planned[0].text
    assert "m × n" in planned[0].text
    assert planned[1].reason == "environment:aligned"

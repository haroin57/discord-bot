"""Choose a Discord-friendly presentation for parsed TeX math.

Discord cannot place uploaded images inline with prose.  Rendering every
``MathSegment`` therefore turns short expressions into a tall sequence of
messages.  This module keeps simple, losslessly-normalisable expressions in
the surrounding text and reserves image rendering for genuinely two-
dimensional notation.

The module deliberately does not import :mod:`utils.markdown_parser`.  The
planner accepts segment-like objects by their public attributes, which keeps
the presentation layer usable from both the batch and streaming paths without
creating an import cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

__all__ = [
    "MathPresentation",
    "PresentationBlock",
    "PresentationBlockKind",
    "PresentationKind",
    "classify_math",
    "latex_to_discord_text",
    "plan_segments",
]


class PresentationKind(StrEnum):
    """Binary presentation decision for a math expression."""

    TEXT = "text"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class MathPresentation:
    """Presentation decision for one math expression.

    ``text`` is the normalised Discord text for ``TEXT`` and the original TeX
    for ``IMAGE``.  Keeping the original TeX on an image decision lets callers
    pass the result directly to the renderer and use it as an alt-text fallback.
    """

    kind: PresentationKind
    text: str
    reason: str


class PresentationBlockKind(StrEnum):
    """Renderable block produced by :func:`plan_segments`."""

    TEXT = "text"
    MATH_IMAGE = "math_image"
    TABLE_IMAGE = "table_image"


@dataclass(frozen=True, slots=True)
class PresentationBlock:
    """One coalesced Discord output block.

    ``source`` retains the original math/table segment for image renderers.
    It is ``None`` for coalesced text because that text may combine several
    source segments.  ``display_mode`` is meaningful only for math images.
    """

    kind: PresentationBlockKind
    text: str
    reason: str
    source: object | None = None
    display_mode: bool | None = None


# Every TeX environment is kept as an image.  The named set gives a useful
# reason for the common two-dimensional forms; unknown environments are also
# rejected conservatively by the generic environment detector.
_TWO_DIMENSIONAL_ENVIRONMENTS = frozenset(
    {
        "aligned",
        "alignedat",
        "array",
        "bmatrix",
        "Bmatrix",
        "cases",
        "equation",
        "equation*",
        "gather",
        "gathered",
        "matrix",
        "multline",
        "pmatrix",
        "smallmatrix",
        "split",
        "subarray",
        "Vmatrix",
        "vmatrix",
    }
)

_COMPLEX_COMMANDS = frozenset(
    {
        "binom",
        "cfrac",
        "dbinom",
        "dfrac",
        "frac",
        "iint",
        "iiint",
        "iiiint",
        "int",
        "lim",
        "oint",
        "overset",
        "prod",
        "sqrt",
        "substack",
        "sum",
        "tbinom",
        "tfrac",
        "underset",
    }
)

_WRAPPER_COMMANDS = frozenset(
    {
        "bm",
        "boldsymbol",
        "mathbf",
        "mathbb",
        "mathcal",
        "mathit",
        "mathrm",
        "mathsf",
        "mathtt",
        "operatorname",
        "text",
        "textnormal",
        "vec",
    }
)

# Commands which have an unambiguous compact Unicode/plain-text rendering.
# Values containing spaces intentionally separate word-like TeX operators from
# neighbouring identifiers (``\dim\ker A`` -> ``dim ker A``).
_COMMAND_TEXT = {
    # Ellipses and arithmetic.
    "ldots": "…",
    "dots": "…",
    "cdots": "⋯",
    "vdots": "⋮",
    "ddots": "⋱",
    "times": " × ",
    "cdot": " · ",
    "div": " ÷ ",
    "pm": " ± ",
    "mp": " ∓ ",
    "ast": " ∗ ",
    "circ": " ∘ ",
    "oplus": " ⊕ ",
    "otimes": " ⊗ ",
    # Relations and arrows.
    "ne": " ≠ ",
    "neq": " ≠ ",
    "le": " ≤ ",
    "leq": " ≤ ",
    "ge": " ≥ ",
    "geq": " ≥ ",
    "approx": " ≈ ",
    "sim": " ∼ ",
    "simeq": " ≃ ",
    "equiv": " ≡ ",
    "cong": " ≅ ",
    "propto": " ∝ ",
    "in": " ∈ ",
    "notin": " ∉ ",
    "ni": " ∋ ",
    "subset": " ⊂ ",
    "supset": " ⊃ ",
    "subseteq": " ⊆ ",
    "supseteq": " ⊇ ",
    "perp": " ⟂ ",
    "parallel": " ∥ ",
    "to": " → ",
    "rightarrow": " → ",
    "leftarrow": " ← ",
    "leftrightarrow": " ↔ ",
    "Rightarrow": " ⇒ ",
    "Leftarrow": " ⇐ ",
    "Leftrightarrow": " ⇔ ",
    "iff": " ⇔ ",
    "implies": " ⇒ ",
    # Named operators and common symbols.
    "dim": "dim ",
    "ker": "ker ",
    "rank": "rank ",
    "det": "det ",
    "gcd": "gcd ",
    "min": "min ",
    "max": "max ",
    "log": "log ",
    "ln": "ln ",
    "exp": "exp ",
    "sin": "sin ",
    "cos": "cos ",
    "tan": "tan ",
    "infty": "∞",
    "partial": "∂",
    "nabla": "∇",
    "forall": "∀",
    "exists": "∃",
    "nexists": "∄",
    "emptyset": "∅",
    "varnothing": "∅",
    "ell": "ℓ",
    "Re": "Re ",
    "Im": "Im ",
    # Greek letters.
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ϵ",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "ϑ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "omicron": "ο",
    "pi": "π",
    "varpi": "ϖ",
    "rho": "ρ",
    "varrho": "ϱ",
    "sigma": "σ",
    "varsigma": "ς",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "varphi": "ϕ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    # Delimiters.
    "left": "",
    "right": "",
    "langle": "⟨",
    "rangle": "⟩",
    "lvert": "|",
    "rvert": "|",
    "lVert": "‖",
    "rVert": "‖",
    "vert": "|",
    "Vert": "‖",
    # TeX spacing commands.
    "quad": " ",
    "qquad": " ",
    ",": " ",
    ";": " ",
    ":": " ",
    "!": "",
    " ": " ",
}

_BLACKBOARD = {
    "C": "ℂ",
    "H": "ℍ",
    "N": "ℕ",
    "P": "ℙ",
    "Q": "ℚ",
    "R": "ℝ",
    "Z": "ℤ",
}

_SUPERSCRIPT = str.maketrans(
    "0123456789+-=()abcdefghijklmnoprstuvwxyzABDEGIJKLMNOPRTUVW",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ",
)
_SUBSCRIPT = str.maketrans(
    "0123456789+-=()aehijklmnoprstuvx",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
)

_ENVIRONMENT_RE = re.compile(r"\\(?:begin|end)\s*\{([^{}]+)\}")
_COMMAND_RE = re.compile(r"\\([A-Za-z]+|.)", re.DOTALL)
_WRAPPER_RE = re.compile(
    r"\\(bm|boldsymbol|mathbf|mathbb|mathcal|mathit|mathrm|mathsf|mathtt|"
    r"operatorname|text|textnormal|vec)\s*\{([^{}]*)\}"
)
_SCRIPT_RE = re.compile(r"([_^])\s*(?:\{([^{}]*)\}|([A-Za-z0-9]))")
# In TeX, two consecutive backslashes are a line break even when the next
# token starts immediately (``x\\\\y``).  No lookahead is needed.
_EXPLICIT_LINE_BREAK_RE = re.compile(r"\\\\")

_ESCAPED_TEXT = {
    "%": "\ue000",
    "#": "\ue001",
    "$": "\ue002",
    "&": "\ue003",
    "_": "\ue004",
    "{": "\ue005",
    "}": "\ue006",
}
_RESTORE_ESCAPED_TEXT = {value: key for key, value in _ESCAPED_TEXT.items()}


def _braces_balanced(latex: str) -> bool:
    depth = 0
    for index, char in enumerate(latex):
        if char not in "{}":
            continue
        preceding_slashes = 0
        cursor = index - 1
        while cursor >= 0 and latex[cursor] == "\\":
            preceding_slashes += 1
            cursor -= 1
        if preceding_slashes % 2:
            continue
        if char == "{":
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _unescaped_character(latex: str, target: str) -> bool:
    for index, char in enumerate(latex):
        if char != target:
            continue
        preceding_slashes = 0
        cursor = index - 1
        while cursor >= 0 and latex[cursor] == "\\":
            preceding_slashes += 1
            cursor -= 1
        if preceding_slashes % 2 == 0:
            return True
    return False


def _rejection_reason(latex: str) -> str | None:
    if not latex.strip():
        return "empty"
    if not _braces_balanced(latex):
        return "unbalanced-braces"

    environment = _ENVIRONMENT_RE.search(latex)
    if environment:
        name = environment.group(1).strip()
        if name in _TWO_DIMENSIONAL_ENVIRONMENTS:
            return f"environment:{name}"
        return f"environment:{name or 'unknown'}"

    if _EXPLICIT_LINE_BREAK_RE.search(latex):
        return "explicit-line-break"
    if _unescaped_character(latex, "%"):
        return "tex-comment"
    if _unescaped_character(latex, "&"):
        return "alignment-marker"

    commands = [match.group(1) for match in _COMMAND_RE.finditer(latex)]
    for command in commands:
        if command in _COMPLEX_COMMANDS:
            return f"complex-command:{command}"
        if (
            command not in _WRAPPER_COMMANDS
            and command not in _COMMAND_TEXT
            and command not in _ESCAPED_TEXT
        ):
            return f"unknown-command:{command}"

    # A terminal backslash is not captured by _COMMAND_RE and cannot be
    # normalised without guessing the author's intent.
    trailing_slashes = len(latex) - len(latex.rstrip("\\"))
    if trailing_slashes % 2:
        return "malformed-command"
    return None


def _replace_non_wrapper_commands(latex: str) -> str:
    def replace(match: re.Match[str]) -> str:
        command = match.group(1)
        if command in _WRAPPER_COMMANDS:
            return match.group(0)
        if command in _ESCAPED_TEXT:
            return _ESCAPED_TEXT[command]
        return _COMMAND_TEXT[command]

    return _COMMAND_RE.sub(replace, latex)


def _replace_wrapper(match: re.Match[str]) -> str:
    command, body = match.groups()
    if command == "mathbb":
        converted = "".join(_BLACKBOARD.get(char, char) for char in body)
        return converted
    if command == "operatorname":
        return f"{body} "
    if command == "vec":
        return f"{body}\N{COMBINING RIGHT ARROW ABOVE}"
    # Typeface changes do not alter the expression's textual meaning.  Plain
    # text is also more readable than mixing mathematical alphanumeric glyphs
    # with normal Japanese prose in Discord clients.
    return body


def _translate_script(body: str, table: dict[int, str]) -> str | None:
    compact = body.replace(" ", "")
    if not compact:
        return None
    translated = compact.translate(table)
    # str.translate leaves unsupported characters unchanged.  A character is
    # safe only when it was explicitly present in the translation table.
    if any(ord(char) not in table for char in compact):
        return None
    return translated


def _replace_scripts(text: str) -> tuple[str, bool]:
    failed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal failed
        marker = match.group(1)
        body = match.group(2) if match.group(2) is not None else match.group(3)
        table = _SUBSCRIPT if marker == "_" else _SUPERSCRIPT
        converted = _translate_script(body, table)
        if converted is None:
            failed = True
            return match.group(0)
        return converted

    return _SCRIPT_RE.sub(replace, text), failed


def _normalise_math(latex: str) -> tuple[str | None, str | None]:
    reason = _rejection_reason(latex)
    if reason is not None:
        return None, reason

    text = _replace_non_wrapper_commands(latex.strip())

    # Wrappers and scripts can nest in either order:
    # ``\boldsymbol{a_{i}}`` needs its script reduced first, while
    # ``x_{\mathrm{max}}`` needs its wrapper reduced first.  Alternating the
    # two bounded passes handles both without implementing a TeX parser.
    script_failed = False
    for _ in range(32):
        before = text
        text = _WRAPPER_RE.sub(_replace_wrapper, text)
        text, failed = _replace_scripts(text)
        script_failed = script_failed or failed
        if text == before:
            break

    if "\\" in text:
        return None, "normalization-failed"
    if "_" in text or "^" in text or script_failed:
        return None, "unsupported-script"

    # Remaining braces are ordinary TeX grouping.  Parentheses preserve the
    # grouping instead of silently discarding it.
    text = text.replace("{", "(").replace("}", ")")
    text = text.replace("~", " ")
    for sentinel, escaped in _RESTORE_ESCAPED_TEXT.items():
        text = text.replace(sentinel, escaped)

    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*([=≠≤≥≈≃≡≅∝∈∉∋⊂⊃⊆⊇⟂∥×÷±∓∗∘⊕⊗→←↔⇒⇐⇔])\s*", r" \1 ", text)
    text = re.sub(r"\s*\+\s*", " + ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return None, "empty-normalization"
    return text, None


def latex_to_discord_text(latex: str) -> str | None:
    """Convert safe, one-dimensional TeX to readable Discord text.

    ``None`` means the expression must remain an image.  The function never
    emits partially-normalised TeX or silently drops an unknown command.
    """

    if not isinstance(latex, str):
        raise TypeError("latex must be a string")
    text, _ = _normalise_math(latex)
    return text


def _math_parts(
    math_or_latex: object,
    display_mode: bool | None,
) -> tuple[str, bool]:
    if isinstance(math_or_latex, str):
        latex = math_or_latex
        mode = bool(display_mode) if display_mode is not None else False
        return latex, mode

    if not hasattr(math_or_latex, "latex") or not hasattr(
        math_or_latex, "display_mode"
    ):
        raise TypeError("math value must be a string or expose latex and display_mode")
    latex = getattr(math_or_latex, "latex")
    if not isinstance(latex, str):
        raise TypeError("math.latex must be a string")
    mode = (
        bool(display_mode)
        if display_mode is not None
        else bool(getattr(math_or_latex, "display_mode"))
    )
    return latex, mode


def classify_math(
    math_or_latex: object,
    display_mode: bool | None = None,
) -> MathPresentation:
    """Classify a TeX string or ``MathSegment``-like object.

    Display delimiters alone do not force image rendering.  A short equation
    emitted as ``$$A=0$$`` is still one-dimensional and remains inlineable.
    """

    latex, mode = _math_parts(math_or_latex, display_mode)
    text, rejection = _normalise_math(latex)
    if text is None:
        return MathPresentation(
            kind=PresentationKind.IMAGE,
            text=latex,
            reason=rejection or "normalization-failed",
        )
    return MathPresentation(
        kind=PresentationKind.TEXT,
        text=text,
        reason="simple-display" if mode else "simple-inline",
    )


def plan_segments(segments: Iterable[object]) -> list[PresentationBlock]:
    """Plan and coalesce parsed text, math, and table segments.

    Segment types are recognised structurally:

    * math: ``latex`` and ``display_mode`` attributes;
    * table: ``markdown`` attribute;
    * text: ``content`` attribute or a raw string.

    Simple math is concatenated into its neighbouring text with no artificial
    whitespace.  The parser's original whitespace therefore remains the sole
    authority for sentence layout.
    """

    blocks: list[PresentationBlock] = []
    text_parts: list[str] = []
    text_contains_math = False

    def flush_text() -> None:
        nonlocal text_contains_math
        if not text_parts:
            return
        combined = "".join(text_parts)
        text_parts.clear()
        if combined.strip():
            blocks.append(
                PresentationBlock(
                    kind=PresentationBlockKind.TEXT,
                    text=combined,
                    reason=("coalesced-text" if text_contains_math else "plain-text"),
                )
            )
        text_contains_math = False

    for segment in segments:
        if isinstance(segment, str):
            text_parts.append(segment)
            continue

        if hasattr(segment, "latex") and hasattr(segment, "display_mode"):
            decision = classify_math(segment)
            if decision.kind is PresentationKind.TEXT:
                text_parts.append(decision.text)
                text_contains_math = True
                continue

            flush_text()
            blocks.append(
                PresentationBlock(
                    kind=PresentationBlockKind.MATH_IMAGE,
                    text=decision.text,
                    reason=decision.reason,
                    source=segment,
                    display_mode=bool(getattr(segment, "display_mode")),
                )
            )
            continue

        if hasattr(segment, "markdown"):
            markdown = getattr(segment, "markdown")
            if not isinstance(markdown, str):
                raise TypeError("table.markdown must be a string")
            flush_text()
            blocks.append(
                PresentationBlock(
                    kind=PresentationBlockKind.TABLE_IMAGE,
                    text=markdown,
                    reason="table",
                    source=segment,
                )
            )
            continue

        if hasattr(segment, "content"):
            content = getattr(segment, "content")
            if not isinstance(content, str):
                raise TypeError("text.content must be a string")
            text_parts.append(content)
            continue

        raise TypeError(
            f"segment must be text-, math-, or table-like; got {type(segment).__name__}"
        )

    flush_text()
    return blocks

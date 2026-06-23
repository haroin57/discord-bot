"""Tests for services/claude_api/progress.py — the CLI-style spinner."""

from services.claude_api.progress import (
    SPINNER_WORDS,
    ProgressSpinner,
    _fmt_tokens,
    pick_word,
    shimmer,
)


def test_word_list_is_substantial_and_clean():
    assert len(SPINNER_WORDS) > 140
    # Authentic CLI words present.
    for w in ("Pondering", "Reticulating", "Clauding", "Noodling"):
        assert w in SPINNER_WORDS
    # No markdown-breaking backslashes (shimmer wraps in **bold**).
    assert all("\\" not in w for w in SPINNER_WORDS)


def test_pick_word_in_list():
    assert pick_word() in SPINNER_WORDS


def test_shimmer_slides_two_char_bold_window():
    assert shimmer("Pondering", 0) == "**Po**ndering"
    assert shimmer("Pondering", 2) == "Po**nd**ering"
    assert shimmer("Pondering", 7) == "Ponderi**ng**"


def test_shimmer_clamps_at_word_end():
    # pos at the last char → window clamps to the final 1-2 chars, no overflow.
    out = shimmer("Doing", 4)
    assert out.startswith("Doin")
    assert "**" in out
    assert out.replace("**", "") == "Doing"


def test_shimmer_empty_and_edges():
    assert shimmer("", 0) == ""
    # Negative pos clamps to 0.
    assert shimmer("Baking", -3) == "**Ba**king"


def test_fmt_tokens_k_abbreviation():
    assert _fmt_tokens(0) == "0"
    assert _fmt_tokens(999) == "999"
    assert _fmt_tokens(1000) == "1.0k"
    assert _fmt_tokens(1234) == "1.2k"
    assert _fmt_tokens(12345) == "12.3k"


def test_spinner_step_rerolls_word_after_sweep():
    s = ProgressSpinner()
    s.word = "Doing"  # len 5
    s.pos = 0
    seen_positions = []
    for _ in range(len(s.word) + 2):
        seen_positions.append(s.pos)
        s.step()
    # After the bold window traverses the word, pos resets to 0 (re-roll).
    assert 0 in seen_positions
    assert s.pos < len(s.word)  # never runs off the end


def test_render_includes_elapsed_tokens_and_hint():
    s = ProgressSpinner()
    s.word = "Pondering"
    s.pos = 2
    line = s.render(elapsed_s=12, tokens=1234, interrupt_hint="/abort で中断")
    assert "Po**nd**ering…" in line
    assert "12s" in line
    assert "1.2k tokens" in line
    assert "/abort で中断" in line


def test_render_omits_tokens_when_zero():
    s = ProgressSpinner()
    s.word = "Working"
    s.pos = 0
    line = s.render(elapsed_s=5, tokens=0)
    assert "tokens" not in line
    assert "5s" in line

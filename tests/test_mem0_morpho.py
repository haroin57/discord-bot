"""Tests for services.mem0.morpho (Janome wrapper)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_tokenizer():
    from services.mem0 import morpho
    yield
    morpho._reset_tokenizer_for_test()


class TestTokenizeKeywords:
    def test_extracts_content_words(self):
        from services.mem0 import morpho
        out = morpho.tokenize_keywords("先輩はRustが好き")
        # particles "は" / "が" must drop; "Rust" / "好き" must stay.
        # "先輩" depends on dictionary but should also appear.
        assert "Rust" in out
        assert "好き" in out
        assert "は" not in out
        assert "が" not in out

    def test_drops_1char_noise(self):
        from services.mem0 import morpho
        out = morpho.tokenize_keywords("ぼ し い な")
        assert out == []  # all 1-char fragments dropped

    def test_returns_empty_on_empty_input(self):
        from services.mem0 import morpho
        assert morpho.tokenize_keywords("") == []
        assert morpho.tokenize_keywords("   ") == []

    def test_returns_empty_on_pure_punctuation(self):
        from services.mem0 import morpho
        out = morpho.tokenize_keywords("、。？！")
        assert out == []

    def test_handles_english(self):
        from services.mem0 import morpho
        out = morpho.tokenize_keywords("TAKU INOUE が好き")
        assert "TAKU" in out
        assert "INOUE" in out
        assert "好き" in out


class TestTokenizeForIndex:
    def test_returns_space_joined_keywords(self):
        from services.mem0 import morpho
        out = morpho.tokenize_for_index("先輩はRustが好き")
        # Result must be whitespace-joined, no quotes / no JSON.
        assert " " in out
        assert "Rust" in out
        assert "好き" in out

    def test_falls_back_to_raw_when_no_keywords(self):
        from services.mem0 import morpho
        # All-particle string yields no keywords; fall back to source so
        # we never write an empty cell.
        out = morpho.tokenize_for_index("、。？！")
        assert out == "、。？！"


class TestExtractQueryKeywords:
    def test_returns_space_joined_string(self):
        from services.mem0 import morpho
        out = morpho.extract_query_keywords("先輩はRustのことが好き？")
        assert "Rust" in out
        assert "好き" in out
        assert " " in out

    def test_caps_at_max_tokens(self):
        from services.mem0 import morpho
        long_query = "Python Rust Go JavaScript TypeScript Java C++ Haskell OCaml Scheme Lisp Erlang"
        out = morpho.extract_query_keywords(long_query, max_tokens=3)
        assert len(out.split()) == 3

    def test_dedupes_repeated_keywords(self):
        from services.mem0 import morpho
        # "Rust" mentioned twice; should appear once in output.
        out = morpho.extract_query_keywords("Rust が好き、 Rust すごい")
        tokens = out.split()
        assert tokens.count("Rust") == 1

    def test_returns_empty_when_no_content(self):
        from services.mem0 import morpho
        assert morpho.extract_query_keywords("") == ""
        assert morpho.extract_query_keywords("、？") == ""

"""Tests for utils/response_sanitizer.py — numeric inline-code stripping."""

import pytest

from utils.response_sanitizer import strip_numeric_inline_code


class TestStripNumericInlineCode:
    def test_strips_currency_with_japanese(self):
        assert strip_numeric_inline_code(
            "年収は `746,404（約1.1億円）` です"
        ) == "年収は 746,404（約1.1億円） です"

    def test_strips_range_expressions(self):
        assert strip_numeric_inline_code(
            "`300K-` から `800K〜` まで"
        ) == "300K- から 800K〜 まで"

    def test_strips_percentage_and_decimal(self):
        assert strip_numeric_inline_code(
            "上昇率は `15%` で `0.05` です"
        ) == "上昇率は 15% で 0.05 です"

    def test_strips_currency_dollar(self):
        assert strip_numeric_inline_code(
            "`$100M+` の調達"
        ) == "$100M+ の調達"

    def test_preserves_python_call(self):
        text = "関数 `range(10)` を使う"
        assert strip_numeric_inline_code(text) == text

    def test_preserves_if_statement(self):
        text = "`if x > 0:` のとき"
        assert strip_numeric_inline_code(text) == text

    def test_preserves_file_path(self):
        text = "パス `/tmp/foo_123.png` を確認"
        assert strip_numeric_inline_code(text) == text

    def test_preserves_dollar_math(self):
        text = "$x^2 + 100$ を解く"
        assert strip_numeric_inline_code(text) == text

    def test_preserves_fenced_code(self):
        text = "```python\nx = 100\n```"
        assert strip_numeric_inline_code(text) == text

    def test_preserves_english_word_no_digit(self):
        text = "`equity` という単語"
        assert strip_numeric_inline_code(text) == text

    def test_empty_string(self):
        assert strip_numeric_inline_code("") == ""

    def test_no_backticks(self):
        text = "プレーンテキストのみ"
        assert strip_numeric_inline_code(text) == text

    def test_mixed_strip_and_preserve(self):
        text = "結果: `100` 件、ヘッダ: `| A | B |`"
        result = strip_numeric_inline_code(text)
        assert "100" in result and "`100`" not in result
        assert "`| A | B |`" in result

    def test_preserves_dotted_identifier(self):
        text = "モジュール `os.path` を import"
        assert strip_numeric_inline_code(text) == text

    def test_preserves_double_backtick(self):
        text = "値は ``literal `text` inside`` です"
        assert strip_numeric_inline_code(text) == text

    def test_strips_number_with_unit_letters(self):
        assert strip_numeric_inline_code(
            "`400K + equity`"
        ) == "400K + equity"

    @pytest.mark.parametrize("inner", [
        "1",
        "100",
        "1,234,567",
        "3.14",
        "$50",
        "¥1000",
        "15%",
        "300K-400K",
        "$1.05M+",
        "800K〜",
    ])
    def test_strips_purely_numeric_tokens(self, inner):
        assert strip_numeric_inline_code(f"x `{inner}` y") == f"x {inner} y"

    @pytest.mark.parametrize("inner", [
        "range(10)",
        "if x:",
        "os.path",
        "my_var",
        "x -> y",
        "/tmp/file",
        "import sys",
        "equity",
    ])
    def test_preserves_code_like_tokens(self, inner):
        text = f"x `{inner}` y"
        assert strip_numeric_inline_code(text) == text

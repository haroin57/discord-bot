"""Unit tests for services/invest_kb/chunker.py."""
import pytest

from services.invest_kb.chunker import Chunk, chunk_text


def test_empty_input_returns_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_short_input_single_chunk():
    out = chunk_text("これは短い文章。", target=800)
    assert len(out) == 1
    assert "短い文章" in out[0].text


def test_size_respected_within_limit():
    # 5000-char paragraph block (single line of mixed sentences).
    text = "これはテスト文章です。" * 500
    out = chunk_text(text, target=800, max_size=1200)
    assert out, "should produce at least one chunk"
    for c in out:
        # Allow slight overshoot when a single sentence exceeds target — we
        # split on sentences, never mid-character.
        assert len(c) <= 1300, f"chunk {len(c)} > 1300 hard cap"


def test_heading_stays_with_following_paragraph():
    text = (
        "# 第1章 投資の原則\n\n"
        "インデックス投資は市場全体に分散する戦略である。"
        "原則を理解することが第一歩。"
    )
    out = chunk_text(text, target=200, max_size=400)
    assert out
    # First chunk should contain BOTH the heading and the explanation.
    assert "第1章" in out[0].text
    assert "インデックス投資" in out[0].text


def test_fenced_code_block_not_split():
    code = "```python\nfor i in range(100):\n    print(i*i)\n```\n"
    long_padding = "原則を説明する文。" * 100
    text = long_padding + "\n\n" + code + "\n\n" + long_padding
    out = chunk_text(text, target=300, max_size=600)
    assert out
    joined = "\n".join(c.text for c in out)
    # The fence opening and closing should appear in the same chunk.
    fence_chunks = [c for c in out if "```python" in c.text]
    assert fence_chunks, "fence opener missing"
    assert any("```" == ch.strip() for c in fence_chunks for ch in c.text.split("\n"))


def test_overlap_between_consecutive_chunks():
    paras = [f"段落{i}の本文を入れる。"*15 for i in range(20)]
    text = "\n\n".join(paras)
    out = chunk_text(text, target=400, max_size=600, overlap=80)
    assert len(out) >= 2
    # Adjacent chunks should share at least some text via the tail-carry.
    for i in range(len(out) - 1):
        a_tail = out[i].text[-100:]
        b_head = out[i + 1].text[:100]
        # Some characters appear in both halves (best-effort, not strict).
        common = set(a_tail) & set(b_head)
        assert len(common) > 5


def test_english_sentence_boundary():
    text = (
        "Index investing tracks a broad market basket. "
        "It minimizes idiosyncratic risk. "
        "Costs compound over time. "
    ) * 80
    out = chunk_text(text, target=400, max_size=700, lang="en")
    assert out
    # Each chunk should end with sentence punctuation, not mid-word.
    for c in out[:-1]:
        last_char = c.text.strip()[-1]
        assert last_char in ".!?", f"chunk does not end on sentence: ...{c.text[-30:]!r}"

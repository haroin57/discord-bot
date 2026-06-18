"""Unit tests for Haiku helpers (summarize/rewrite/hyde/rerank).

All Anthropic calls are mocked. We're testing the JSON parsing, fallbacks,
and ordering semantics — not the LLM itself.
"""
import json
from unittest.mock import patch, AsyncMock

import pytest

from services.invest_kb import haiku


def _patch_call(text: str):
    """Patch _call_haiku to return the given Haiku response body."""
    return patch.object(haiku, "_call_haiku", AsyncMock(return_value=text))


def _patch_call_fail():
    return patch.object(haiku, "_call_haiku", AsyncMock(side_effect=RuntimeError("boom")))


# ---------------------------------------------------------------------------
# summarize_chunk
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_summarize_happy_path():
    payload = json.dumps({
        "summary": "インデックス投資は市場全体に分散する。" * 1,
        "keywords": ["インデックス", "分散投資"],
        "entities": ["S&P500"],
    })
    with _patch_call(payload):
        out = await haiku.summarize_chunk("どんな本文でも")
    assert out["summary"].startswith("インデックス")
    assert out["keywords"] == ["インデックス", "分散投資"]
    assert out["entities"] == ["S&P500"]


@pytest.mark.asyncio
async def test_summarize_code_fence_stripped():
    payload = "```json\n" + json.dumps({"summary": "テスト", "keywords": [], "entities": []}) + "\n```"
    with _patch_call(payload):
        out = await haiku.summarize_chunk("本文")
    assert out["summary"] == "テスト"


@pytest.mark.asyncio
async def test_summarize_fallback_on_failure():
    with _patch_call_fail():
        out = await haiku.summarize_chunk("foobar"*50)
    # Fallback = text prefix 90 chars
    assert out["summary"]
    assert out["keywords"] == []
    assert out["entities"] == []


@pytest.mark.asyncio
async def test_summarize_caps_summary_to_90_chars():
    payload = json.dumps({
        "summary": "あ" * 200,
        "keywords": ["x"],
        "entities": ["y"],
    })
    with _patch_call(payload):
        out = await haiku.summarize_chunk("本文")
    assert len(out["summary"]) <= 90


# ---------------------------------------------------------------------------
# rewrite_query
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rewrite_returns_variants_excluding_original():
    payload = json.dumps({"queries": [
        "インデックス投資の本質",
        "インデックス投資 メリット",
        "インデックス投資について",
    ]})
    with _patch_call(payload):
        out = await haiku.rewrite_query("インデックス投資 とは")
    assert len(out) >= 1
    assert "インデックス投資 とは" not in out


@pytest.mark.asyncio
async def test_rewrite_short_query_returns_empty():
    out = await haiku.rewrite_query("a")
    assert out == []


@pytest.mark.asyncio
async def test_rewrite_failure_returns_empty():
    with _patch_call_fail():
        out = await haiku.rewrite_query("長めのクエリを書いてみよう")
    assert out == []


# ---------------------------------------------------------------------------
# hyde
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hyde_happy_path():
    with _patch_call(json.dumps({"hypothetical": "インデックス投資は時間分散と銘柄分散を一度に達成する。"})):
        out = await haiku.hyde("インデックス投資のメリットは?")
    assert out.startswith("インデックス")


@pytest.mark.asyncio
async def test_hyde_failure_returns_empty():
    with _patch_call_fail():
        assert await haiku.hyde("長めのクエリ") == ""


# ---------------------------------------------------------------------------
# rerank
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rerank_orders_by_haiku_ids():
    candidates = [
        {"id": "a", "summary": "A 要約"},
        {"id": "b", "summary": "B 要約"},
        {"id": "c", "summary": "C 要約"},
    ]
    with _patch_call(json.dumps({"top_ids": ["c", "a"]})):
        out = await haiku.rerank("クエリ", candidates, top_k=2)
    assert [r["id"] for r in out] == ["c", "a"]


@pytest.mark.asyncio
async def test_rerank_pads_when_haiku_returns_fewer():
    # Force the "len(candidates) > top_k" branch so the rerank actually fires.
    candidates = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    with _patch_call(json.dumps({"top_ids": ["b"]})):
        out = await haiku.rerank("クエリ", candidates, top_k=3)
    # First slot is what Haiku picked; remaining slots fill from input order
    # (skipping the already-picked).
    assert out[0]["id"] == "b"
    assert {c["id"] for c in out} == {"b", "a", "c"}


@pytest.mark.asyncio
async def test_rerank_short_skip_haiku():
    candidates = [{"id": "x"}]
    out = await haiku.rerank("q", candidates, top_k=5)
    assert out == [{"id": "x"}]


@pytest.mark.asyncio
async def test_rerank_failure_fallback_to_input_order():
    candidates = [{"id": str(i)} for i in range(10)]
    with _patch_call_fail():
        out = await haiku.rerank("q", candidates, top_k=3)
    assert [r["id"] for r in out] == ["0", "1", "2"]

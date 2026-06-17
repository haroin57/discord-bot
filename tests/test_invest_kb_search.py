"""Unit tests for search/gate/format helpers of invest_kb."""
import os
from unittest.mock import patch

import pytest
import pyarrow as pa

from services.invest_kb.search import (
    format_for_prompt, is_investment_query, search_invest_kb,
)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "インデックス投資の原則を教えて",
        "新NISA の上限は?",
        "iDeCo と特定口座の使い分け",
        "PER と PBR で何が違うのか",
        "ETF の配当の扱い",
        "what's the dividend yield of TOPIX ETFs?",
    ],
)
def test_gate_fires_on_investment(text):
    assert is_investment_query(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "今日の天気どう?",
        "かなえ可愛い",
        "晩飯何食べた?",
        "Ableton Live のメロディが綺麗に組めた",
        "pytestを直したい",
        "",
    ],
)
def test_gate_silent_on_chitchat(text):
    assert is_investment_query(text) is False


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------
def test_format_for_prompt_truncates():
    rows = [
        {"id": "edinet:E1:0", "memory": "a" * 200, "metadata": {"source": "edinet", "source_id": "E1"}},
        {"id": "edinet:E2:0", "memory": "b" * 200, "metadata": {"source": "edinet", "source_id": "E2"}},
        {"id": "edinet:E3:0", "memory": "c" * 200, "metadata": {"source": "edinet", "source_id": "E3"}},
    ]
    out = format_for_prompt(rows, max_chars=300)
    assert "edinet:E1" in out
    assert "edinet:E3" not in out  # truncated


def test_format_for_prompt_with_title():
    rows = [{
        "id": "hayatoito:2020:0",
        "memory": "インデックス投資の原則",
        "metadata": {"source": "hayatoito", "source_id": "2020", "title": "資産運用99点"},
    }]
    out = format_for_prompt(rows)
    assert "hayatoito" in out
    assert "資産運用99点" in out


def test_format_for_prompt_empty():
    assert format_for_prompt([]) == ""


# ---------------------------------------------------------------------------
# search_invest_kb: empty-table fast path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_empty_table_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("config.INVEST_KB_PATH", str(tmp_path / "kb"))
    # client cache may have been populated by another test — clear it.
    from services.invest_kb import client as kb_client
    kb_client._db_cache["db"] = None
    kb_client._db_cache["path"] = None

    out = await search_invest_kb("どんなクエリでも", limit=5, use_haiku=False)
    assert out == []


# ---------------------------------------------------------------------------
# search_invest_kb: full pipeline (mocked Haiku + mocked embed) on a seeded
# in-memory LanceDB.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_returns_mem0_shape(tmp_path, monkeypatch):
    monkeypatch.setattr("config.INVEST_KB_PATH", str(tmp_path / "kb"))
    from services.invest_kb import client as kb_client
    kb_client._db_cache["db"] = None
    kb_client._db_cache["path"] = None
    tbl = kb_client.get_table()

    # Seed 3 rows with deterministic vectors.
    dim = kb_client.EMBED_DIM
    rows = []
    for i, label in enumerate(("alpha", "beta", "gamma")):
        v = [0.0] * dim
        v[i] = 1.0  # one-hot so cosine distance is monotone in matching index
        rows.append({
            "id": f"test:{label}:0",
            "text": f"{label} body text",
            "summary": f"{label} 要約",
            "keywords": "",
            "entities": "",
            "vector_body": v,
            "vector_summary": v,
            "source": "test",
            "source_id": label,
            "source_url": "",
            "title": label.upper(),
            "lang": "ja",
            "license": "test",
            "published_at": 0,
            "category": "test",
        })
    tbl.add(rows)

    # Mock embed_batch to return a vector that picks "alpha" (one-hot index 0).
    async def fake_embed(texts):
        v = [0.0] * dim
        v[0] = 1.0
        return [v for _ in texts]
    # search.py does a local import; patch only at the source.
    monkeypatch.setattr("services.invest_kb.embed.embed_batch", fake_embed)

    out = await search_invest_kb("alpha please", limit=2, use_haiku=False)
    assert out, "expected hits"
    # First hit should be the matching one.
    assert out[0]["id"] == "test:alpha:0"
    # Mem0-shaped: id, memory, score, metadata
    keys = set(out[0].keys())
    assert keys >= {"id", "memory", "score", "metadata"}
    assert out[0]["metadata"]["source"] == "test"
    assert out[0]["metadata"]["title"] == "ALPHA"

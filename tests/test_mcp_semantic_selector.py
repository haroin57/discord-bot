"""Tests for services/mcp_semantic_selector.py（埋め込みベース MCP selector）。

OpenAI 埋め込みは全て monkeypatch（決定的ベクトル）。ChromaDB 不要。
"""

import json
from unittest.mock import AsyncMock

import pytest

from services import mcp_semantic_selector as sel


_SCHEMAS = {
    "scholarly": [{"name": "mcp__scholarly__search",
                   "description": "学術論文の検索"}],
    "playwright": [{"name": "mcp__playwright__navigate",
                    "description": "ブラウザ操作とスクリーンショット"}],
    "realestate": [{"name": "mcp__realestate__search",
                    "description": "賃貸物件の検索"}],
}

# サーバー順（dict順）: scholarly, playwright, realestate
_SERVER_VECS = {
    "scholarly": [1.0, 0.0, 0.0],
    "playwright": [0.0, 1.0, 0.0],
    "realestate": [0.0, 0.0, 1.0],
}


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    sel._reset_for_test()
    monkeypatch.setattr(sel, "_INDEX_PATH", str(tmp_path / "idx.json"))
    monkeypatch.setattr(
        "services.claude_api.mcp_client.STATIC_MCP_TOOL_SCHEMAS", _SCHEMAS,
    )
    monkeypatch.setattr(sel, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(sel, "MCP_SEMANTIC_MIN_SIM", 0.5)
    monkeypatch.setattr(sel, "MCP_SEMANTIC_TOP_K", 3)
    monkeypatch.setattr(sel, "MCP_SERVERS", {
        k: {"command": "x"} for k in _SCHEMAS
    })

    async def fake_embed(texts):
        out = []
        for t in texts:
            for key, vec in _SERVER_VECS.items():
                if key in t:
                    out.append(list(vec))
                    break
            else:
                # クエリ: キーワードでベクトルを決める
                if "論文" in t:
                    out.append([0.9, 0.1, 0.0])
                elif "スクショ" in t:
                    out.append([0.1, 0.9, 0.0])
                elif "物件" in t:
                    out.append([0.0, 0.1, 0.9])
                else:
                    out.append([0.0, 0.0, 0.0])
        return out

    monkeypatch.setattr(sel, "_embed", fake_embed)
    yield
    sel._reset_for_test()


class TestSemanticPick:
    @pytest.mark.asyncio
    async def test_picks_similar_server(self):
        out = await sel.pick_mcp_servers_semantic("この論文調べて", user_id=1)
        assert list(out) == ["scholarly"]

    @pytest.mark.asyncio
    async def test_below_threshold_returns_empty(self):
        out = await sel.pick_mcp_servers_semantic("こんにちは", user_id=1)
        assert out == {}

    @pytest.mark.asyncio
    async def test_permission_gate_blocks_realestate(self, monkeypatch):
        monkeypatch.setattr(sel, "ADMIN_USER_IDS", {42})
        out = await sel.pick_mcp_servers_semantic("いい物件ない？", user_id=99)
        assert "realestate" not in out
        out_admin = await sel.pick_mcp_servers_semantic("いい物件ない？", user_id=42)
        assert "realestate" in out_admin

    @pytest.mark.asyncio
    async def test_empty_text_or_no_key(self, monkeypatch):
        assert await sel.pick_mcp_servers_semantic("", user_id=1) == {}
        monkeypatch.setattr(sel, "OPENAI_API_KEY", "")
        assert await sel.pick_mcp_servers_semantic("論文", user_id=1) == {}

    @pytest.mark.asyncio
    async def test_embed_failure_returns_empty(self, monkeypatch):
        # index build 前に embed が死ぬ → 空 dict（例外は上げない）
        monkeypatch.setattr(
            sel, "_embed", AsyncMock(side_effect=RuntimeError("down")),
        )
        assert await sel.pick_mcp_servers_semantic("論文調べて", user_id=1) == {}


class TestIndexCache:
    @pytest.mark.asyncio
    async def test_index_persisted_and_reused(self, tmp_path):
        await sel.pick_mcp_servers_semantic("論文調べて", user_id=1)
        with open(sel._INDEX_PATH, encoding="utf-8") as f:
            data = json.load(f)
        assert set(data["servers"]) == set(_SCHEMAS)
        # 再起動相当: メモリ index を消してもファイルから復元され、
        # ハッシュ一致なら再埋め込みされない
        sel._reset_for_test()
        calls = {"n": 0}
        orig = sel._embed

        async def counting_embed(texts):
            calls["n"] += 1
            return await orig(texts)

        sel._embed, _saved = counting_embed, orig
        try:
            out = await sel.pick_mcp_servers_semantic("論文調べて", user_id=1)
        finally:
            sel._embed = _saved
        assert list(out) == ["scholarly"]
        # クエリ埋め込みの1回だけ（サーバー側は再埋め込み無し）
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_changed_description_reembedded(self, monkeypatch):
        await sel.pick_mcp_servers_semantic("論文調べて", user_id=1)
        sel._reset_for_test()
        changed = dict(_SCHEMAS)
        changed["scholarly"] = [{"name": "mcp__scholarly__search",
                                 "description": "changed scholarly desc"}]
        monkeypatch.setattr(
            "services.claude_api.mcp_client.STATIC_MCP_TOOL_SCHEMAS", changed,
        )
        out = await sel.pick_mcp_servers_semantic("論文調べて", user_id=1)
        # scholarly のテキストに 'scholarly' が含まれるので fake_embed は
        # サーバーベクトルを返す → 引き続き選択可能（再埋め込みが通った証左）
        assert list(out) == ["scholarly"]
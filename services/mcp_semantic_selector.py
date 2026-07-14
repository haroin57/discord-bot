"""Embedding-based MCP server selection (layer 2 of the tool-call stack).

キーワード部分一致（services/mcp_selector.py）の誤爆・取りこぼしを補うため、
「メッセージ本文の埋め込み × 各 MCP サーバーの tool description 埋め込み」の
cosine 類似で eager 広告するサーバーを選ぶ。サーバー側ベクトルは
``.state/mcp_selector_embeddings.json`` に永続キャッシュし、description の
ハッシュが変わった分だけ再埋め込みする。埋め込みモデルは Mem0 と同じ
text-embedding-3-small（OPENAI_API_KEY）。

失敗（API 障害・未ビルド・key 無し）は例外を上げず空 dict を返し、呼び出し側
（mcp_selector.select_mcp_servers）が keyword 選択へフォールバックする。
Tool Search（層1）有効時はここで選ばれたサーバーが hot set（非 defer 広告 +
eager spawn）になり、漏れても検索で発見可能という二段構えになる。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os

from config import (
    ADMIN_USER_IDS,
    MCP_SEMANTIC_MIN_SIM,
    MCP_SEMANTIC_TOP_K,
    MCP_SERVERS,
    NATURE_REMO_ALLOWED_USER_IDS,
    OPENAI_API_KEY,
    STATE_DIR,
)
from utils.atomic_write import atomic_write_json
from utils.cache import TTLLRUCache
from services.mcp_control import filter_enabled_mcp_configs

log = logging.getLogger(__name__)

_EMBED_MODEL = os.environ.get("MCP_SELECTOR_EMBED_MODEL", "text-embedding-3-small")
_INDEX_PATH = os.path.join(STATE_DIR, "mcp_selector_embeddings.json")
_TEXT_CAP = 4000       # per-server description text cap（8192 token 制限の保険）
_QUERY_CAP = 2000      # メッセージ側の埋め込み対象文字数
_EMBED_RETRIES = 3

# server key -> (text_hash, vector) のメモリキャッシュ。ビルドは per-process。
_index: dict[str, tuple[str, list[float]]] = {}
_index_lock = asyncio.Lock()
_index_ready = False
# 直近メッセージの埋め込みキャッシュ（連投・リトライ吸収）。
_query_cache: TTLLRUCache[str, list[float]] = TTLLRUCache(maxsize=256, ttl_s=600.0)

_openai_client = None


def _get_client():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        _openai_client = AsyncOpenAI()
    return _openai_client


async def _embed(texts: list[str]) -> list[list[float]]:
    """Batch embed with bounded retry（invest_kb/embed.py と同パターン）。"""
    client = _get_client()
    delay = 1.0
    for attempt in range(_EMBED_RETRIES):
        try:
            resp = await client.embeddings.create(model=_EMBED_MODEL, input=texts)
            return [d.embedding for d in resp.data]
        except Exception as exc:
            if attempt == _EMBED_RETRIES - 1:
                raise
            log.warning(
                "mcp_selector embed failed (attempt %d/%d): %s",
                attempt + 1, _EMBED_RETRIES, exc,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)
    return []


def _server_embedding_text(key: str, tools: list[dict]) -> str:
    parts = [key]
    for t in tools:
        parts.append(t.get("name", ""))
        parts.append(t.get("description", ""))
    return "\n".join(p for p in parts if p)[:_TEXT_CAP]


def _load_index_file() -> dict:
    try:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("model") == _EMBED_MODEL:
            return data.get("servers", {}) or {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


async def _ensure_index() -> bool:
    """サーバー側ベクトルを（差分だけ）構築する。成功で True。"""
    global _index_ready
    if _index_ready:
        return True
    async with _index_lock:
        if _index_ready:
            return True
        # 遅延 import（circular 回避: mcp_client → config は entry 経由で重い）
        from services.claude_api.mcp_client import STATIC_MCP_TOOL_SCHEMAS

        stored = _load_index_file()
        missing: list[tuple[str, str, str]] = []  # (key, hash, text)
        for key, tools in STATIC_MCP_TOOL_SCHEMAS.items():
            text = _server_embedding_text(key, tools)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cached = stored.get(key)
            if (
                isinstance(cached, dict)
                and cached.get("hash") == digest
                and isinstance(cached.get("vector"), list)
            ):
                _index[key] = (digest, cached["vector"])
            else:
                missing.append((key, digest, text))
        if missing:
            try:
                vectors = await _embed([t for _, _, t in missing])
            except Exception as exc:
                log.warning("mcp_selector index build failed: %s", exc)
                # 部分的に読めた分だけで動かす（空なら未ready）
                _index_ready = bool(_index)
                return _index_ready
            for (key, digest, _), vec in zip(missing, vectors):
                _index[key] = (digest, vec)
            try:
                atomic_write_json(
                    _INDEX_PATH,
                    {
                        "model": _EMBED_MODEL,
                        "servers": {
                            k: {"hash": h, "vector": v}
                            for k, (h, v) in _index.items()
                        },
                    },
                )
            except OSError as exc:
                log.warning("mcp_selector index save failed (non-fatal): %s", exc)
            log.info(
                "mcp_selector index built: %d servers (%d re-embedded)",
                len(_index), len(missing),
            )
        _index_ready = bool(_index)
        return _index_ready


def _server_allowed(key: str, user_id: int | None) -> bool:
    if key == "realestate":
        return user_id is not None and user_id in ADMIN_USER_IDS
    if key == "nature-remo":
        return user_id is not None and user_id in NATURE_REMO_ALLOWED_USER_IDS
    return True


def _cosine_scores(qvec: list[float]) -> list[tuple[str, float]]:
    import numpy as np

    keys = list(_index.keys())
    matrix = np.asarray([_index[k][1] for k in keys], dtype=np.float32)
    q = np.asarray(qvec, dtype=np.float32)
    # OpenAI embeddings はほぼ単位ベクトルだが防御的に正規化
    mnorm = np.linalg.norm(matrix, axis=1, keepdims=True)
    mnorm[mnorm == 0] = 1.0
    qnorm = np.linalg.norm(q) or 1.0
    sims = (matrix / mnorm) @ (q / qnorm)
    return list(zip(keys, (float(s) for s in sims)))


async def pick_mcp_servers_semantic(
    raw_user_text: str, user_id: int | None = None,
) -> dict[str, dict]:
    """埋め込み類似で MCP サーバーを選ぶ。失敗・低類似は空 dict。"""
    text = (raw_user_text or "").strip()[:_QUERY_CAP]
    if not text or not OPENAI_API_KEY:
        return {}
    if not await _ensure_index():
        return {}
    cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    qvec = _query_cache.get(cache_key)
    if qvec is None:
        try:
            qvec = (await _embed([text]))[0]
        except Exception as exc:
            log.warning("mcp_selector query embed failed: %s", exc)
            return {}
        _query_cache.set(cache_key, qvec)
    scored = [
        (k, s) for k, s in _cosine_scores(qvec)
        if s >= MCP_SEMANTIC_MIN_SIM
        and _server_allowed(k, user_id)
        and k in MCP_SERVERS
    ]
    scored.sort(key=lambda x: -x[1])
    chosen = scored[: max(int(MCP_SEMANTIC_TOP_K), 0)]
    if chosen:
        log.info(
            "mcp_selector semantic: %s",
            ", ".join(f"{k}={s:.3f}" for k, s in chosen),
        )
    return filter_enabled_mcp_configs({k: MCP_SERVERS[k] for k, _ in chosen})


def _reset_for_test() -> None:
    global _index_ready
    _index.clear()
    _index_ready = False
    _query_cache.clear()

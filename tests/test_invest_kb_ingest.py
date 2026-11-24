"""Atomic replacement and async I/O contracts for invest_kb."""

from __future__ import annotations

import asyncio
import threading
import time

import pandas as pd
import pytest

from services.invest_kb import client
from services.invest_kb.ingest import upsert_documents
from services.invest_kb.search import _vector_search


def _chunk_row(
    source_id: str,
    index: int,
    text: str,
    *,
    source: str = "test",
    row_id: str | None = None,
) -> dict:
    vector = [0.0] * client.EMBED_DIM
    vector[index % client.EMBED_DIM] = 1.0
    return {
        "id": row_id or f"{source}:{source_id}:{index}",
        "text": text,
        "summary": text,
        "keywords": "",
        "entities": "",
        "vector_body": vector,
        "vector_summary": vector,
        "source": source,
        "source_id": source_id,
        "source_url": "",
        "title": source_id,
        "lang": "ja",
        "license": "test",
        "published_at": 0,
        "category": "test",
    }


async def _read_document(path: str, source_id: str) -> list[dict]:
    def _read() -> list[dict]:
        table = client.get_table(path)
        frame = table.to_pandas()
        rows = frame[frame["source_id"] == source_id]
        return rows.sort_values("id").to_dict(orient="records")

    return await asyncio.to_thread(_read)


async def _read_all(path: str) -> list[dict]:
    def _read() -> list[dict]:
        table = client.get_table(path)
        return table.to_pandas().sort_values("id").to_dict(orient="records")

    return await asyncio.to_thread(_read)


@pytest.fixture(autouse=True)
def _reset_client_cache():
    with client._db_cache_lock:
        client._db_cache["db"] = None
        client._db_cache["path"] = None
    yield
    with client._db_cache_lock:
        client._db_cache["db"] = None
        client._db_cache["path"] = None


def test_chunk_id_is_stable_and_preserves_field_boundaries():
    first = client.make_chunk_id("a:b", "c", 0)
    second = client.make_chunk_id("a", "b:c", 0)

    assert first == (
        "invest-kb:v2:866d149b5f21ccb5fb29c3eb326de5dc66779ccc2ab79b3dab7a14a0d551003f"
    )
    assert first == client.make_chunk_id("a:b", "c", 0)
    assert first.startswith(client.CHUNK_ID_PREFIX)
    assert first != second


@pytest.mark.asyncio
async def test_colliding_legacy_document_keys_have_distinct_v2_rows(tmp_path):
    path = str(tmp_path / "kb")
    assert ":".join(("a:b", "c", "0")) == ":".join(("a", "b:c", "0"))
    first_id = client.make_chunk_id("a:b", "c", 0)
    second_id = client.make_chunk_id("a", "b:c", 0)

    await client.replace_document(
        [_chunk_row("c", 0, "first", source="a:b", row_id=first_id)],
        path=path,
        retry_delay_s=0,
    )
    await client.replace_document(
        [_chunk_row("b:c", 0, "second", source="a", row_id=second_id)],
        path=path,
        retry_delay_s=0,
    )

    rows = await _read_all(path)
    assert {(row["source"], row["source_id"], row["id"]) for row in rows} == {
        ("a:b", "c", first_id),
        ("a", "b:c", second_id),
    }


@pytest.mark.asyncio
async def test_replace_document_migrates_legacy_ids_atomically_and_idempotently(
    tmp_path,
):
    path = str(tmp_path / "kb")
    legacy_rows = [
        _chunk_row("doc", 0, "legacy-0"),
        _chunk_row("doc", 1, "legacy-1"),
    ]
    await client.replace_document(legacy_rows, path=path, retry_delay_s=0)
    await client.replace_document(
        [_chunk_row("other", 0, "untouched")],
        path=path,
        retry_delay_s=0,
    )

    replacement = [
        _chunk_row(
            "doc",
            0,
            "v2",
            row_id=client.make_chunk_id("test", "doc", 0),
        ),
    ]
    await client.replace_document(replacement, path=path, retry_delay_s=0)
    await client.replace_document(replacement, path=path, retry_delay_s=0)

    doc_rows = await _read_document(path, "doc")
    other_rows = await _read_document(path, "other")
    assert [(row["id"], row["text"]) for row in doc_rows] == [
        (client.make_chunk_id("test", "doc", 0), "v2"),
    ]
    assert [(row["id"], row["text"]) for row in other_rows] == [
        ("test:other:0", "untouched"),
    ]


@pytest.mark.asyncio
async def test_failed_legacy_id_migration_keeps_previous_version(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "kb")
    legacy_rows = [
        _chunk_row("doc", 0, "legacy-0"),
        _chunk_row("doc", 1, "legacy-1"),
    ]
    await client.replace_document(legacy_rows, path=path, retry_delay_s=0)

    def _fail(_rows, *, path=None):
        raise OSError("injected migration failure")

    monkeypatch.setattr(client, "_replace_document_sync", _fail)
    with pytest.raises(OSError, match="injected migration failure"):
        await client.replace_document(
            [
                _chunk_row(
                    "doc",
                    0,
                    "v2",
                    row_id=client.make_chunk_id("test", "doc", 0),
                ),
            ],
            path=path,
            max_attempts=1,
            retry_delay_s=0,
        )

    current = await _read_document(path, "doc")
    assert [(row["id"], row["text"]) for row in current] == [
        ("test:doc:0", "legacy-0"),
        ("test:doc:1", "legacy-1"),
    ]


@pytest.mark.asyncio
async def test_replace_document_atomically_removes_stale_chunks_and_is_idempotent(
    tmp_path,
):
    path = str(tmp_path / "kb")
    await client.replace_document(
        [_chunk_row("doc", 0, "old-0"), _chunk_row("doc", 1, "old-1")],
        path=path,
        retry_delay_s=0,
    )
    await client.replace_document(
        [_chunk_row("other", 0, "untouched")],
        path=path,
        retry_delay_s=0,
    )

    replacement = [_chunk_row("doc", 0, "new-0")]
    await client.replace_document(replacement, path=path, retry_delay_s=0)
    await client.replace_document(replacement, path=path, retry_delay_s=0)

    doc_rows = await _read_document(path, "doc")
    other_rows = await _read_document(path, "other")
    assert [(row["id"], row["text"]) for row in doc_rows] == [
        ("test:doc:0", "new-0"),
    ]
    assert [(row["id"], row["text"]) for row in other_rows] == [
        ("test:other:0", "untouched"),
    ]


@pytest.mark.asyncio
async def test_replace_document_failure_preserves_old_document(tmp_path, monkeypatch):
    path = str(tmp_path / "kb")
    old_rows = [_chunk_row("doc", 0, "old-0"), _chunk_row("doc", 1, "old-1")]
    await client.replace_document(old_rows, path=path, retry_delay_s=0)

    calls = 0

    def _fail(_rows, *, path=None):
        nonlocal calls
        calls += 1
        raise OSError("injected merge failure")

    monkeypatch.setattr(client, "_replace_document_sync", _fail)
    with pytest.raises(OSError, match="injected merge failure"):
        await client.replace_document(
            [_chunk_row("doc", 0, "new")],
            path=path,
            max_attempts=3,
            retry_delay_s=0,
        )

    assert calls == 3
    current = await _read_document(path, "doc")
    assert [(row["id"], row["text"]) for row in current] == [
        ("test:doc:0", "old-0"),
        ("test:doc:1", "old-1"),
    ]


@pytest.mark.asyncio
async def test_replace_document_retries_uncertain_commit_idempotently(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "kb")
    await client.replace_document(
        [_chunk_row("doc", 0, "old-0"), _chunk_row("doc", 1, "old-1")],
        path=path,
        retry_delay_s=0,
    )
    real_replace = client._replace_document_sync
    calls = 0

    def _commit_then_fail(rows, *, path=None):
        nonlocal calls
        calls += 1
        result = real_replace(rows, path=path)
        if calls == 1:
            raise ConnectionError("injected lost acknowledgement")
        return result

    monkeypatch.setattr(client, "_replace_document_sync", _commit_then_fail)
    result = await client.replace_document(
        [_chunk_row("doc", 0, "new")],
        path=path,
        max_attempts=2,
        retry_delay_s=0,
    )

    assert result["attempts"] == 2
    current = await _read_document(path, "doc")
    assert [(row["id"], row["text"]) for row in current] == [
        ("test:doc:0", "new"),
    ]


@pytest.mark.asyncio
async def test_concurrent_replacements_for_same_document_are_serialized(
    tmp_path,
    monkeypatch,
):
    active = 0
    max_active = 0
    calls = 0
    guard = threading.Lock()

    def _tracked_replace(_rows, *, path=None):
        nonlocal active, max_active, calls
        with guard:
            active += 1
            calls += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return None

    monkeypatch.setattr(client, "_replace_document_sync", _tracked_replace)
    rows = [_chunk_row("doc", 0, "same")]
    path = str(tmp_path / "kb")
    await asyncio.gather(
        client.replace_document(rows, path=path, retry_delay_s=0),
        client.replace_document(rows, path=path, retry_delay_s=0),
    )

    assert calls == 2
    assert max_active == 1


@pytest.mark.asyncio
async def test_replace_document_does_not_block_event_loop(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def _blocking_replace(_rows, *, path=None):
        started.set()
        assert release.wait(timeout=1)
        return None

    monkeypatch.setattr(client, "_replace_document_sync", _blocking_replace)
    task = asyncio.create_task(
        client.replace_document(
            [_chunk_row("doc", 0, "new")],
            path=str(tmp_path / "kb"),
            retry_delay_s=0,
        ),
    )
    await asyncio.sleep(0.02)

    assert started.is_set()
    assert not task.done()
    release.set()
    await task


@pytest.mark.asyncio
async def test_table_open_does_not_block_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def _blocking_get_table(path=None):
        started.set()
        assert release.wait(timeout=1)
        return object()

    monkeypatch.setattr(client, "get_table", _blocking_get_table)
    task = asyncio.create_task(client.get_table_async("unused"))
    await asyncio.sleep(0.02)

    assert started.is_set()
    assert not task.done()
    release.set()
    assert await task is not None


@pytest.mark.asyncio
async def test_vector_search_does_not_block_event_loop():
    started = threading.Event()
    release = threading.Event()

    class _Query:
        def metric(self, _name):
            return self

        def limit(self, _limit):
            return self

        def select(self, _columns):
            return self

        def to_list(self):
            started.set()
            assert release.wait(timeout=1)
            return []

    class _Table:
        def search(self, _vec, *, vector_column_name):
            return _Query()

    task = asyncio.create_task(_vector_search(_Table(), [1.0], "vector_body", 5))
    await asyncio.sleep(0.02)

    assert started.is_set()
    assert not task.done()
    release.set()
    assert await task == []


@pytest.mark.asyncio
async def test_upsert_documents_calls_atomic_contract_per_document(monkeypatch):
    async def _summarize(chunks_text, lang, concurrency=10):
        return [
            {"summary": text, "keywords": [], "entities": []} for text in chunks_text
        ]

    async def _embed(texts):
        return [[0.0] * client.EMBED_DIM for _ in texts]

    replacements: list[list[dict]] = []

    async def _replace(rows, **_kwargs):
        replacements.append(rows)
        return {"chunks": len(rows)}

    monkeypatch.setattr("services.invest_kb.ingest._summarize_all", _summarize)
    monkeypatch.setattr("services.invest_kb.embed.embed_batch", _embed)
    monkeypatch.setattr("services.invest_kb.client.replace_document", _replace)

    result = await upsert_documents(
        [
            {"text": "first", "source": "test", "source_id": "one", "lang": "ja"},
            {"text": "second", "source": "test", "source_id": "two", "lang": "ja"},
        ]
    )

    assert result["documents"] == 2
    assert len(replacements) == 2
    assert [{row["source_id"] for row in rows} for rows in replacements] == [
        {"one"},
        {"two"},
    ]
    assert [rows[0]["id"] for rows in replacements] == [
        client.make_chunk_id("test", "one", 0),
        client.make_chunk_id("test", "two", 0),
    ]


@pytest.mark.asyncio
async def test_incomplete_embedding_batch_fails_before_document_replace(monkeypatch):
    async def _summarize(chunks_text, lang, concurrency=10):
        return [
            {"summary": text, "keywords": [], "entities": []} for text in chunks_text
        ]

    calls = 0

    async def _embed(_texts):
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        return [[0.0] * client.EMBED_DIM]

    async def _replace(_rows, **_kwargs):
        raise AssertionError("replace must not run after incomplete preparation")

    monkeypatch.setattr("services.invest_kb.ingest._summarize_all", _summarize)
    monkeypatch.setattr("services.invest_kb.embed.embed_batch", _embed)
    monkeypatch.setattr("services.invest_kb.client.replace_document", _replace)

    with pytest.raises(RuntimeError, match="embedder returned an incomplete"):
        await upsert_documents(
            [
                {"text": "first", "source": "test", "source_id": "one", "lang": "ja"},
            ]
        )

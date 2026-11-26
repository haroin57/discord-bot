from unittest.mock import AsyncMock, Mock

import pytest

from services.mem0 import client


async def test_strict_listing_unavailable_differs_from_empty(monkeypatch):
    monkeypatch.setattr(client, "_get_mem0", Mock(return_value=None))
    assert await client.get_all_memories("owner") == []
    with pytest.raises(RuntimeError):
        await client.get_all_memories("owner", strict=True)


@pytest.mark.parametrize("raw", [42, {"error": "unavailable"}, {"results": None}])
async def test_strict_listing_rejects_malformed_results(monkeypatch, raw):
    monkeypatch.setattr(client, "_get_mem0", Mock(return_value=Mock(get_all=Mock(return_value=raw))))
    monkeypatch.setattr(client, "ensure_valid_token", AsyncMock())
    monkeypatch.setattr(client, "_refresh_mem0_client_token", Mock())
    with pytest.raises(RuntimeError):
        await client.get_all_memories("owner", strict=True)


async def test_strict_listing_empty_is_a_verified_result(monkeypatch):
    backend = Mock(get_all=Mock(return_value={"results": []}))
    monkeypatch.setattr(client, "_get_mem0", Mock(return_value=backend))
    monkeypatch.setattr(client, "ensure_valid_token", AsyncMock())
    monkeypatch.setattr(client, "_refresh_mem0_client_token", Mock())
    assert await client.get_all_memories("owner", strict=True) == []
    backend.get_all.assert_called_once_with(user_id="owner")

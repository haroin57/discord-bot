"""Tests for services/claude_api.py — normalize_messages, OAuth client, streaming."""

import json
import time
from unittest.mock import MagicMock, patch, mock_open

import pytest

from services.claude_api import (
    normalize_messages,
)


class TestNormalizeMessages:
    def test_empty(self):
        assert normalize_messages([]) == []

    def test_no_merge_needed(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        assert normalize_messages(msgs) == msgs

    def test_merges_consecutive_user_strings(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 1
        assert "first" in result[0]["content"]
        assert "second" in result[0]["content"]

    def test_merges_consecutive_user_lists_with_hoist(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "result1"},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "world"},
            ]},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 1
        content = result[0]["content"]
        assert content[0]["type"] == "tool_result"
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert len(text_blocks) == 2

    def test_does_not_merge_different_roles(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 3

    def test_does_not_merge_mixed_types(self):
        msgs = [
            {"role": "user", "content": "string"},
            {"role": "user", "content": [{"type": "text", "text": "list"}]},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 2

    def test_multiple_consecutive_merges(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        result = normalize_messages(msgs)
        assert len(result) == 1
        assert "a" in result[0]["content"]
        assert "c" in result[0]["content"]


class TestTokenExpiry:
    """Tests for oauth_manager._is_expired() which replaced _is_token_expired."""

    def test_past_is_expired(self, monkeypatch):
        import services.oauth_manager as om
        past_ms = int(time.time() * 1000) - 1000
        monkeypatch.setattr(om, "_expires_at", past_ms)
        assert om._is_expired() is True

    def test_within_buffer_is_expired(self, monkeypatch):
        import services.oauth_manager as om
        # 200s from now < 300s (EXPIRY_BUFFER_MS) => still expired
        almost_expired_ms = int(time.time() * 1000) + 200_000
        monkeypatch.setattr(om, "_expires_at", almost_expired_ms)
        assert om._is_expired() is True

    def test_future_is_not_expired(self, monkeypatch):
        import services.oauth_manager as om
        future_ms = int(time.time() * 1000) + 600_000  # 10 min from now
        monkeypatch.setattr(om, "_expires_at", future_ms)
        assert om._is_expired() is False

    def test_zero_expires_at_is_expired(self, monkeypatch):
        import services.oauth_manager as om
        monkeypatch.setattr(om, "_expires_at", 0)
        assert om._is_expired() is True


class TestLoadOAuthTokens:
    @pytest.mark.skip(reason="_load_oauth_tokens moved to oauth_manager; not exported from claude_api")
    def test_loads_from_credentials(self):
        pass

    @pytest.mark.skip(reason="_load_oauth_tokens moved to oauth_manager; not exported from claude_api")
    def test_raises_on_missing_file(self):
        pass


class TestGetClient:
    def test_creates_client(self, monkeypatch):
        from services.claude_api import get_client
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_cached_client", None)
        monkeypatch.setattr(api_mod, "_cached_token", None)
        monkeypatch.setattr("services.oauth_manager.init", lambda: None)
        monkeypatch.setattr("services.oauth_manager.get_access_token", lambda: "sk-test")
        client = get_client()
        assert client is not None

    def test_caches_client(self, monkeypatch):
        from services.claude_api import get_client
        import services.claude_api as api_mod
        monkeypatch.setattr(api_mod, "_cached_client", None)
        monkeypatch.setattr(api_mod, "_cached_token", None)
        monkeypatch.setattr("services.oauth_manager.init", lambda: None)
        monkeypatch.setattr("services.oauth_manager.get_access_token", lambda: "sk-cached")
        c1 = get_client()
        c2 = get_client()
        assert c1 is c2

    def test_refreshes_on_token_change(self, monkeypatch):
        from services.claude_api import get_client
        import services.claude_api as api_mod
        old_client = MagicMock()
        monkeypatch.setattr(api_mod, "_cached_client", old_client)
        monkeypatch.setattr(api_mod, "_cached_token", "sk-old")
        monkeypatch.setattr("services.oauth_manager.init", lambda: None)
        monkeypatch.setattr("services.oauth_manager.get_access_token", lambda: "sk-new")
        client = get_client()
        assert client is not old_client

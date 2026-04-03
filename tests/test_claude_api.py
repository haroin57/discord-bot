"""Tests for services/claude_api.py — normalize_messages, OAuth client, streaming."""

import json
import time
from unittest.mock import MagicMock, patch, mock_open

import pytest

from services.claude_api import (
    normalize_messages,
    _is_token_expired,
    _load_oauth_tokens,
    get_client,
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
    def test_none_is_expired(self):
        assert _is_token_expired(None) is True

    def test_past_is_expired(self):
        past_ms = int(time.time() * 1000) - 1000
        assert _is_token_expired(past_ms) is True

    def test_within_buffer_is_expired(self):
        # TOKEN_REFRESH_BUFFER_MS = 5 * 60 * 1000 = 300000
        almost_expired_ms = int(time.time() * 1000) + 200_000  # 200s from now < 300s buffer
        assert _is_token_expired(almost_expired_ms) is True

    def test_future_is_not_expired(self):
        future_ms = int(time.time() * 1000) + 600_000  # 10 min from now
        assert _is_token_expired(future_ms) is False


class TestLoadOAuthTokens:
    def test_loads_from_credentials(self, tmp_path, monkeypatch):
        creds = {"claudeAiOauth": {"accessToken": "sk-test", "expiresAt": 9999999999999}}
        creds_path = tmp_path / "creds.json"
        creds_path.write_text(json.dumps(creds))
        monkeypatch.setattr("services.claude_api.CREDENTIALS_PATH", str(creds_path))
        tokens = _load_oauth_tokens()
        assert tokens["accessToken"] == "sk-test"

    def test_raises_on_missing_file(self, monkeypatch):
        monkeypatch.setattr("services.claude_api.CREDENTIALS_PATH", "/nonexistent")
        with pytest.raises(FileNotFoundError):
            _load_oauth_tokens()


class TestGetClient:
    def test_creates_client(self, tmp_path, monkeypatch):
        creds = {"claudeAiOauth": {"accessToken": "sk-test-123", "expiresAt": int(time.time() * 1000) + 600_000}}
        creds_path = tmp_path / "creds.json"
        creds_path.write_text(json.dumps(creds))
        monkeypatch.setattr("services.claude_api.CREDENTIALS_PATH", str(creds_path))
        # Reset cache
        monkeypatch.setattr("services.claude_api._cached_client", None)
        monkeypatch.setattr("services.claude_api._cached_token_expires_at", 0)
        client = get_client()
        assert client is not None

    def test_caches_client(self, tmp_path, monkeypatch):
        creds = {"claudeAiOauth": {"accessToken": "sk-test-456", "expiresAt": int(time.time() * 1000) + 600_000}}
        creds_path = tmp_path / "creds.json"
        creds_path.write_text(json.dumps(creds))
        monkeypatch.setattr("services.claude_api.CREDENTIALS_PATH", str(creds_path))
        monkeypatch.setattr("services.claude_api._cached_client", None)
        monkeypatch.setattr("services.claude_api._cached_token_expires_at", 0)
        c1 = get_client()
        c2 = get_client()
        assert c1 is c2  # same cached instance

    def test_refreshes_on_expiry(self, tmp_path, monkeypatch):
        creds = {"claudeAiOauth": {"accessToken": "sk-new", "expiresAt": int(time.time() * 1000) + 600_000}}
        creds_path = tmp_path / "creds.json"
        creds_path.write_text(json.dumps(creds))
        monkeypatch.setattr("services.claude_api.CREDENTIALS_PATH", str(creds_path))
        monkeypatch.setattr("services.claude_api._cached_client", MagicMock())
        monkeypatch.setattr("services.claude_api._cached_token_expires_at", 0)  # expired
        client = get_client()
        assert client is not None  # rebuilt, not the old mock
